"""Land profile: invert each tile's water polygons into whatever's left of
a buffered tile square after subtracting them. See docs/EXAMPLE_PROFILES.md
("land") for the full rationale, including why the square is buffered
past the tile edge and why the water union gets a snap-rounding difference.
"""
import shapely
from shapely.geometry import box

from tilealchemist import mvt, water
from tilealchemist.profiles.base import Profile

# Extent assumed for a real tile with no water layer at all, or for a gap
# tile (no archive data whatsoever, so there's no per-tile extent to read).
GAP_TILE_EXTENT = 4096


class LandProfile(Profile):
    name = "land"
    output_layer_name = "land"
    mbtiles_name = "land"

    def __init__(self, schema):
        self.schema = schema
        self._gap_tile_cache = None

    def vector_layers_json(self):
        return [{"id": self.output_layer_name, "fields": {}}]

    def _buffered_square(self, extent):
        buffer = extent * self.schema.default_buffer_pixels / self.schema.tile_size_pixels
        return box(-buffer, -buffer, extent + buffer, extent + buffer)

    def _encode_land_tile(self, land, extent):
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

        return mvt.encode_tile(self.output_layer_name, [{"geometry": land, "properties": {}}], extent)

    def transform_tile_bytes(self, data):
        """Decode one tile's water layer and return gzipped MVT bytes for
        the inverted land layer, or None if the tile is entirely water
        (skipped, same as a missing tile in any vector tileset)."""
        decoded = mvt.decode_tile(data)
        surface = self.schema.surface_water(decoded)
        extent = surface.extent if surface is not None else GAP_TILE_EXTENT
        square = self._buffered_square(extent)

        union = water.union_polygons(surface.polygons) if surface is not None else None
        if union is not None:
            # Forces GEOS's fixed-precision (snap-rounding) overlay instead
            # of the default floating-point one. The float overlay computes
            # edge intersections with ordinary float arithmetic; on geometry
            # with near-coincident or near-collinear points (buffer(0)
            # leftovers, water nearly touching the tile square) two edges
            # that should meet at one point can resolve to two float-apart
            # points instead, so the polygonizer can't match a hole ring to
            # its shell and throws (seen live on real OpenFreeMap water
            # geometry, e.g. build 20260726_080001_pt around z9/x274/y147).
            # Snap-rounding pre-snaps coordinates to a fixed grid before
            # computing intersections, so that inconsistency can't arise.
            land = square.difference(union, grid_size=water.OUTPUT_GRID_SIZE)
        else:
            land = square

        return self._encode_land_tile(land, extent)

    def gap_tile_bytes(self, zoom, tile_column, tile_row):
        """Gap entries are tile_ids absent from the archive entirely, not
        just missing a water layer, so every one is an identical bare
        square regardless of location: (zoom, tile_column, tile_row) go
        unused here, this profile has no location-dependent gap policy."""
        if self._gap_tile_cache is None:
            square = self._buffered_square(GAP_TILE_EXTENT)
            self._gap_tile_cache = self._encode_land_tile(square, GAP_TILE_EXTENT)
        return self._gap_tile_cache
