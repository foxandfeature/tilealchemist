"""Cropped-waterways profile: keep each tile's `waterway` line features only
where they don't overlap real water polygons. Motivation: a style that uses
the land profile as its base layer (instead of painting water on top of
plain background) needs waterway lines pre-cropped to the land side, or a
river/stream stroke would visibly double up with the water polygon it
already runs through. See docs/EXAMPLE_PROFILES.md for the full rationale.
"""
import shapely
from shapely.geometry import GeometryCollection, LineString, MultiLineString, shape

from tilealchemist import water
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
    output_layer_name = "cropped-waterways"
    mbtiles_name = "cropped-waterways"
    compatible_schemas = None  # only calls TileSchema's universal API
                                # (surface_water/waterway_lines/waterway_fields/
                                # default_buffer_pixels/tile_size_pixels)

    def __init__(self, schema):
        self.schema = schema

    def vector_layers_json(self):
        return [{"id": self.output_layer_name, "fields": self.schema.waterway_fields()}]

    def transform_layer(self, decoded_tile):
        """Decode one tile's waterway layer and return (features, extent)
        for the cropped lines, or None if the tile has no waterway features
        at all (skipped, same as a missing tile in any vector tileset)."""
        waterway = self.schema.waterway_lines(decoded_tile)
        if waterway is None:
            return None
        extent = waterway.extent
        union = water.surface_water_union(decoded_tile, self.schema)
        if union is not None:
            # grid_size= on difference() alone isn't enough here: unlike
            # land's polygon-polygon case, GEOS's precision-reducing overlay
            # for a *line* against a polygon can still throw a
            # TopologyException ("side location conflict") on real-world OSM
            # water geometry with near-coincident points. Snapping the union
            # onto the output grid ourselves first, the same topology-aware
            # way land.py already relies on, removes those near-duplicate
            # points before the overlay ever runs instead of hoping the
            # overlay survives them.
            union = shapely.set_precision(union, water.OUTPUT_GRID_SIZE, mode="valid_output")

        features = []
        for feature in waterway.features:
            geometry = shape(feature["geometry"])
            if union is not None:
                geometry = shapely.set_precision(
                    geometry, water.OUTPUT_GRID_SIZE, mode="valid_output")
                geometry = geometry.difference(union, grid_size=water.OUTPUT_GRID_SIZE)
            cropped = _line_components(geometry)
            cropped = shapely.set_precision(cropped, water.OUTPUT_GRID_SIZE, mode="valid_output")
            if cropped.is_empty:
                continue
            features.append({"geometry": cropped, "properties": feature["properties"]})

        if not features:
            return None

        return features, extent

    def gap_tile_bytes(self, zoom, tile_column, tile_row):
        """A gap tile means the source archive had nothing at all for this
        tile_id (no water, no waterway), so there's no faithful "cropped
        waterway" content to invent, unlike land's well-defined "whole
        square minus nothing" case. True for every location, so
        (zoom, tile_column, tile_row) go unused."""
        return None


PROFILE = CroppedWaterwaysProfile
