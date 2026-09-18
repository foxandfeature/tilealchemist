"""Thin MVT (Mapbox Vector Tile) codec.

The source archive's tiles and this pipeline's mbtiles output rows are both
gzipped MVT. Backs `Tile.decode()` and `Profile.transform_tile()`, so a
profile normally never calls this module directly.

Also owns OUTPUT_GRID_SIZE and snap_to_output_grid(). The grid is this
encoder's own integer coordinate grid, so `encode_tile()` snaps what it is
given and no profile has to remember to. docs/PROFILES.md "The output grid"
says why that snap is load-bearing and which profiles it defends.
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
    """`geometry` snapped onto OUTPUT_GRID_SIZE, topology-aware.

    Unlike a plain coordinate rounding, mode="valid_output" repairs the
    validity errors rounding itself introduces: pieces that only start
    crossing once each is rounded on its own. What it cannot repair, it
    drops. An element collapsing below one grid unit is removed, so the
    result can be empty; see encode_tile()'s is_empty check.

    Not an optimization. The encoder rounds to integers either way, and this
    decides whether it does so topology-aware."""
    return shapely.set_precision(geometry, OUTPUT_GRID_SIZE, mode="valid_output")


def encode_tile(layer_name, features, extent):
    """One output layer's features -> gzipped MVT tile bytes, or None if no
    feature survives snapping.

    `features` is a list of the {"geometry": <shapely geometry>,
    "properties": dict} dicts mapbox_vector_tile.encode() takes, the shape
    decode_tile() produces. This module deals in the library's own format in
    both directions; `Profile._encode_tile()` is where a `Feature` becomes
    one."""
    # Snapping MUST happen before encoding; everything below depends on it.
    # Unsnapped, near-coincident parts round into an invalid MultiPolygon the
    # library cannot repair (it validates each part in isolation), and
    # on_invalid_geometry_raise aborts the shard. Sub-unit geometry rounds
    # away silently and still gets written as a row holding an empty layer.
    # Both are invisible from a profile's side, which is why this lives here.
    # Measured on tile z9/269/151: even a fixed-precision overlay's output
    # still moves under this snap. docs/PROFILES.md "The output grid" has the
    # full case.
    snapped = []
    for feature in features:
        geometry = snap_to_output_grid(feature["geometry"])
        # Snapping can collapse a feature to nothing: a line or sliver
        # narrower than one grid cell, which overlay results routinely leave
        # behind. Such a feature has no representation at this extent, and
        # dropping it is what lets `if not snapped` return None rather than
        # write an empty layer.
        if geometry.is_empty:
            continue
        # Rebuilt by update, so anything else the caller's dict carries (an
        # "id", say) survives.
        snapped.append({**feature, "geometry": geometry})
    if not snapped:
        return None
    encoded = mapbox_vector_tile.encode(
        {"name": layer_name, "features": snapped},
        default_options={"extents": extent, "on_invalid_geometry": on_invalid_geometry_raise},
    )
    # mtime=0 is REQUIRED. gzip.compress() otherwise embeds the current time,
    # so byte-identical tile content (every gap tile, in particular) would
    # compress differently across worker processes and defeat PMTiles'
    # content-hash dedup in the final merge.
    return gzip.compress(encoded, mtime=0)
