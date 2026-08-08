"""Thin MVT (Mapbox Vector Tile) codec shared by every profile that reads
or writes gzip-compressed vector tile bytes, which in practice is every
profile in this pipeline: the source archive's tiles and this pipeline's
own mbtiles output rows are both gzipped MVT. Backs `Profile`'s default
`transform_tile_bytes()` and its `_encode_tile()` helper (see
`profiles/base.py`) - the shared codec every concrete profile shipped here
relies on without importing this module directly. Still not a hard
requirement of the `Profile` contract: a profile that needs non-MVT output,
or full control over encoding, can override `transform_tile_bytes()`/
`gap_tile_bytes()` directly instead. Not scoped to any one profile's domain
logic the way `water.py` is: any future profile, water-related or not, ends
up needing exactly this decode/encode pair.
"""
import gzip

import mapbox_vector_tile
from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid


def decode_tile(data):
    """One gzipped source MVT tile's bytes -> decoded {layer_name: {...}} dict."""
    return mapbox_vector_tile.decode(gzip.decompress(data))


def encode_tile(layer_name, features, extent):
    """One output layer's features -> gzipped MVT tile bytes. `features` is
    a list of {"geometry": <a geometry object mapbox_vector_tile.encode()
    accepts, e.g. a shapely geometry>, "properties": dict}."""
    encoded = mapbox_vector_tile.encode(
        {"name": layer_name, "features": features},
        default_options={"extents": extent, "on_invalid_geometry": on_invalid_geometry_make_valid},
    )
    # mtime=0: gzip.compress() otherwise embeds the current time, which
    # would make byte-identical tile content (every gap tile, in
    # particular) compress to different bytes across worker processes and
    # defeat PMTiles' own content-hash dedup in the final merge.
    return gzip.compress(encoded, mtime=0)
