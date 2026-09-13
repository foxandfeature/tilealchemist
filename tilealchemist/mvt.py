"""Thin MVT (Mapbox Vector Tile) codec: the source archive's tiles and this
pipeline's own mbtiles output rows are both gzipped MVT. Backs `Tile.decode()`
and `Profile.transform_tile()`, so a profile normally never calls this module
directly.

Also owns OUTPUT_GRID_SIZE and snap_to_output_grid(), since the grid in
question is this encoder's own integer coordinate grid, not anything a profile
invented: `encode_tile()` snaps what it is given, so no profile has to
remember to. docs/PROFILES.md "The output grid" is why that snap is
load-bearing rather than tidiness, and which profiles it is defending.
"""
import gzip

import mapbox_vector_tile
import shapely
from mapbox_vector_tile.encoder import on_invalid_geometry_raise

# The integer grid MVT coordinates are encoded onto: one unit, since an MVT
# tile's coordinates are integers relative to its extent by spec.
OUTPUT_GRID_SIZE = 1.0


def decode_tile(data):
    """One gzipped source MVT tile's bytes -> decoded {layer_name: {...}} dict."""
    return mapbox_vector_tile.decode(gzip.decompress(data))


def snap_to_output_grid(geometry):
    """`geometry` snapped onto OUTPUT_GRID_SIZE, topology-aware: unlike a plain
    coordinate rounding, mode="valid_output" repairs the validity errors that
    rounding itself introduces (pieces that only start crossing once each is
    rounded on its own). What it does not repair, it drops: an element that
    collapses below one grid unit is removed, so the result can be empty; see
    encode_tile()'s is_empty check. Neither an optimization nor
    tidiness: the encoder rounds to integers either way, and this decides
    whether it does so topology-aware."""
    return shapely.set_precision(geometry, OUTPUT_GRID_SIZE, mode="valid_output")


def encode_tile(layer_name, features, extent):
    """One output layer's features -> gzipped MVT tile bytes, or None if no
    feature survives snapping. `features` is a list of the
    {"geometry": <shapely geometry>, "properties": dict} dicts
    mapbox_vector_tile.encode() takes, the same shape decode_tile() produces:
    this module deals in the library's own format in both directions, and
    `Profile._encode_tile()` is where this pipeline's `Feature` becomes one."""
    # The encoder rounds to integers either way; snapping first makes that
    # rounding topology-aware, and everything below depends on it having
    # happened. Unsnapped, near-coincident parts round into an invalid
    # MultiPolygon the library cannot repair (it validates each part in
    # isolation) and on_invalid_geometry_raise aborts the shard, while
    # sub-unit geometry rounds away silently and still gets written as a row
    # holding an empty layer. Both are invisible from a profile's side, which
    # is why this is here and not in each profile, and it is a no-op for
    # nobody: measured on tile z9/269/151, even a fixed-precision overlay's
    # output still moves under this snap, which drops what collapses below a
    # unit as well as rounding. docs/PROFILES.md "The output grid" has the
    # full case, including which profiles it is actually defending.
    snapped = []
    for feature in features:
        geometry = snap_to_output_grid(feature["geometry"])
        # Snapping can collapse a feature to nothing (a line or sliver narrower
        # than one grid cell, which overlay results routinely leave behind).
        # Such a feature has no representation at this extent at all, and
        # dropping it here is what lets `if not snapped` below return None for
        # a tile rather than write an empty layer.
        if geometry.is_empty:
            continue
        # Rebuilt rather than mutated, and by update rather than by hand, so
        # anything else the caller's dict carries (an "id", say) survives.
        snapped.append({**feature, "geometry": geometry})
    if not snapped:
        return None
    encoded = mapbox_vector_tile.encode(
        {"name": layer_name, "features": snapped},
        default_options={"extents": extent, "on_invalid_geometry": on_invalid_geometry_raise},
    )
    # mtime=0: gzip.compress() otherwise embeds the current time, which would
    # make byte-identical tile content (every gap tile, in particular) compress
    # to different bytes across worker processes and defeat PMTiles' own
    # content-hash dedup in the final merge.
    return gzip.compress(encoded, mtime=0)
