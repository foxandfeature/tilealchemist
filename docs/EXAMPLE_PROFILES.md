# Example profiles: `land` and `cropped-waterways`

TileAlchemist is a pipeline, not a land-layer generator: the two profiles
below are what it happens to ship today, not the whole point of the
project. See [`docs/PROFILES.md`](PROFILES.md) for the `Profile` system
itself (the contract every profile implements, and how to write your own).
More profiles, unrelated to water, are expected to show up here over time.

## Why these two exist

OpenFreeMap, and most other OpenMapTiles-schema vector tile providers, ship
a `water` layer but no `land` layer. That's fine for the default styles,
which just paint the map background as land color and draw water on top.
But a custom style that wants to treat land as its own styleable/maskable
layer (a distinct fill, a texture, a land-only overlay) has nothing to
attach it to. [MapTiler's `land` layer](https://docs.maptiler.com/schema/land/)
is the closest existing reference: plain, attribute-less land polygons
meant as a base layer for custom styling. OpenFreeMap doesn't publish an
equivalent, so the **land** profile derives one by inverting the `water`
layer it does publish.

Once land replaces water as a style's base layer, the source's `waterway`
line layer (rivers, streams) starts visually clashing with it: a stroked
line drawn straight through the water polygon it represents now runs across
solid land-colored fill instead. The **cropped-waterways** profile removes
exactly the overlapping portions, so waterway lines only appear where
they're actually on land. See it combined with `land` in
[`examples/cropped-waterways.json`](../examples/cropped-waterways.json).

## `land`

For every tile in the z0..`max_zoom` pyramid, `LandProfile.transform_layer()`
(`tilealchemist/profiles/land.py`), called by the inherited
`Profile.transform_tile_bytes()`:

1. Asks the schema for this tile's real surface water
   (`schema.surface_water(decoded)`, see `TileSchema` in
   [`docs/PROFILES.md`](PROFILES.md)). For `OPENMAPTILES` this reads the
   `water` layer and drops tunnel water (`brunnel == "tunnel"`: a water
   polygon running through a tunnel isn't open water at the surface), and
   hands back each remaining polygon as a raw MVT-decoded geometry dict;
   `LandProfile` itself parses those into shapely (`shapely.geometry.shape`)
   right before unioning them.
2. Unions those polygons via `tilealchemist/water.py:union_polygons()`.
3. Inverts the tile: `land = tile_square - union(remaining water polygons)`,
   entirely in that tile's own local coordinates. `tile_square` is buffered
   4px (64 units at the standard 4096 extent) past the tile edge on every
   side, matching OpenMapTiles/Planetiler's own default buffer for the
   `water` layer, so the buffered land polygon is fully determined by data
   already fetched, no neighboring tiles needed. Without this, a renderer
   stroking the coastline as a thick line sees the polygon end abruptly at
   the tile boundary, producing a visible kink where two tiles meet instead
   of a continuous line.

Because each tile is inverted independently against its own square, there's
no cross-tile geometry work and no re-simplification beyond whatever detail
the source archive already has at that zoom.

Gap tiles (no archive entry at all for that tile_id) get an identical bare
buffered square: full land is the well-defined "nothing to subtract" case.

## `cropped-waterways`

`CroppedWaterwaysProfile.transform_layer()`
(`tilealchemist/profiles/cropped_waterways.py`), called by the inherited
`Profile.transform_tile_bytes()`, reads this tile's waterway line features
via `schema.waterway_lines(decoded)` (each feature's raw MVT-decoded
geometry dict is parsed into shapely by the profile itself, same as `land`),
and the *same* `schema.surface_water(decoded)` + `union_polygons()` result
the land profile would compute for that tile, then keeps only the portion of
each waterway line that doesn't overlap it (`geometry.difference(union)`).
Reusing the exact same union (not recomputing it independently) matters: if
the two profiles' water polygons disagreed even by a sub-pixel amount, a
cropped waterway line and the land polygon it's meant to hug could show a
visible seam once rendered together.

`LineString.difference(Polygon)` at a point of pure tangency (the line just
touches the polygon's edge without truly crossing into it) can return a
`GeometryCollection` mixing the real `LineString`/`MultiLineString` result
with degenerate `Point` pieces. `_line_components()` filters that down to
just its line pieces before encoding.

Gap tiles are skipped entirely for this profile: a gap means the source
archive had no `water` and no `waterway` data at all for that tile_id, so
there's no faithful "cropped waterway" content to invent, unlike land's
well-defined "whole square minus nothing" case.
