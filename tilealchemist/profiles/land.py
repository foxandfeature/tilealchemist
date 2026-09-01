"""Land profile: invert each tile's water polygons into whatever's left of
a buffered tile square after subtracting them. See docs/EXAMPLE_PROFILES.md
("land") for the full rationale, including why the square is buffered
past the tile edge and why the water union gets a snap-rounding difference.
"""
import shapely

from tilealchemist import water
from tilealchemist.profiles.base import Profile

# Extent assumed for a real tile with no water layer at all, or for a gap
# tile (no archive data whatsoever, so there's no per-tile extent to read).
GAP_TILE_EXTENT = 4096


class LandProfile(Profile):
    name = "land"
    output_layer_name = "land"
    mbtiles_name = "land"
    compatible_schemas = None  # only calls TileSchema's universal API
                                # (surface_water/waterway_lines/waterway_fields/
                                # default_buffer_pixels/tile_size_pixels)

    def __init__(self, schema):
        self.schema = schema

    def vector_layers_json(self):
        return [{"id": self.output_layer_name, "fields": {}}]

    def _land_features(self, land):
        # mapbox_vector_tile rounds coordinates to water.OUTPUT_GRID_SIZE per
        # polygon piece, with no awareness of how pieces relate to each
        # other, so pieces valid individually can end up with crossing edges
        # once rounded independently (typically slivers near the tile edge
        # where a water polygon nearly touches the square). Its retry-based
        # repair can't fix that class of error and silently drops the whole
        # feature, so snap to the same grid ourselves first with a
        # topology-aware snap that repairs validity as it rounds.
        land = shapely.set_precision(land, water.OUTPUT_GRID_SIZE, mode="valid_output")

        if land.is_empty:
            return None

        return [{"geometry": land, "properties": {}}]

    def transform_layer(self, decoded_tile):
        """Decode one tile's water layer and return (features, extent) for
        the inverted land layer, or None if the tile is entirely water
        (skipped, same as a missing tile in any vector tileset)."""
        surface = self.schema.surface_water(decoded_tile)
        extent = surface.extent if surface is not None else GAP_TILE_EXTENT
        land = water.land_polygon(decoded_tile, self.schema, extent)

        features = self._land_features(land)
        return (features, extent) if features is not None else None


PROFILE = LandProfile
