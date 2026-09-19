"""Thin MVT codec, and the output grid every encoded tile is snapped onto."""
import gzip

import mapbox_vector_tile
import shapely
from mapbox_vector_tile.encoder import on_invalid_geometry_raise

# One unit: MVT coordinates are integers relative to a tile's extent, by spec.
OUTPUT_GRID_SIZE = 1.0


def decode_tile(data):
    return mapbox_vector_tile.decode(gzip.decompress(data))


def snap_to_output_grid(geometry):
    """Topology-aware, and not an optimization: the encoder rounds either way."""
    return shapely.set_precision(geometry, OUTPUT_GRID_SIZE, mode="valid_output")


def encode_tile(layer_name, features, extent):
    # Must precede encoding; docs/PROFILES.md "The output grid" says what breaks otherwise.
    snapped = []
    for feature in features:
        geometry = snap_to_output_grid(feature["geometry"])
        if geometry.is_empty:
            continue
        snapped.append({**feature, "geometry": geometry})
    if not snapped:
        return None
    encoded = mapbox_vector_tile.encode(
        {"name": layer_name, "features": snapped},
        default_options={"extents": extent, "on_invalid_geometry": on_invalid_geometry_raise},
    )
    # mtime=0 keeps identical tiles byte-identical across workers, for PMTiles dedup.
    return gzip.compress(encoded, mtime=0)
