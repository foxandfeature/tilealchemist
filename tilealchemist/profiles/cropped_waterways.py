"""Cropped-waterways profile: keep each tile's `waterway` line features only
where they don't overlap real water polygons. Motivation: a style that uses
the land profile as its base layer (instead of painting water on top of
plain background) needs waterway lines pre-cropped to the land side, or a
river/stream stroke would visibly double up with the water polygon it
already runs through. See docs/EXAMPLE_PROFILES.md for the full rationale.
"""
import shapely
from shapely.geometry import GeometryCollection, LineString, MultiLineString

from tilealchemist import mvt, water
from tilealchemist.profiles.base import Profile


def _line_components(geometry):
    """geometry.difference(polygon) on a LineString that's merely tangent to
    the polygon (touches at a single point without truly overlapping) can
    return a GeometryCollection mixing LineString/MultiLineString with
    degenerate Point pieces at the tangent point, which
    mapbox_vector_tile.encode() doesn't handle the same way as a clean
    line geometry. Keep only the line parts."""
    if isinstance(geometry, (LineString, MultiLineString)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        parts = []
        for piece in geometry.geoms:
            if isinstance(piece, LineString):
                parts.append(piece)
            elif isinstance(piece, MultiLineString):
                parts.extend(piece.geoms)
        return MultiLineString(parts) if parts else LineString()
    return LineString()


class CroppedWaterwaysProfile(Profile):
    name = "cropped-waterways"
    output_layer_name = "waterways"
    mbtiles_name = "waterways"

    def __init__(self, schema):
        self.schema = schema

    def vector_layers_json(self):
        return [{"id": self.output_layer_name, "fields": self.schema.waterway_fields()}]

    def transform_tile_bytes(self, data):
        """Decode one tile's waterway layer and return gzipped MVT bytes for
        the cropped lines, or None if the tile has no waterway features at
        all (skipped, same as a missing tile in any vector tileset)."""
        decoded = mvt.decode_tile(data)
        waterway = self.schema.waterway_lines(decoded)
        if waterway is None:
            return None
        extent = waterway.extent
        surface = self.schema.surface_water(decoded)
        union = water.union_polygons(surface.polygons) if surface is not None else None

        features = []
        for feature in waterway.features:
            geometry = feature["geometry"]
            cropped = geometry.difference(union, grid_size=water.OUTPUT_GRID_SIZE) if union is not None else geometry
            cropped = _line_components(cropped)
            cropped = shapely.set_precision(cropped, water.OUTPUT_GRID_SIZE, mode="valid_output")
            if cropped.is_empty:
                continue
            features.append({"geometry": cropped, "properties": feature["properties"]})

        if not features:
            return None

        return mvt.encode_tile(self.output_layer_name, features, extent)

    def gap_tile_bytes(self, zoom, tile_column, tile_row):
        """A gap tile means the source archive had nothing at all for this
        tile_id (no water, no waterway), so there's no faithful "cropped
        waterway" content to invent, unlike land's well-defined "whole
        square minus nothing" case. True for every location, so
        (zoom, tile_column, tile_row) go unused."""
        return None
