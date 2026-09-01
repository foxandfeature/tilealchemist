"""Cropped-waterways profile: keep each tile's `waterway` line features only
where they don't overlap real water polygons. Motivation: a style that uses
the land profile as its base layer (instead of painting water on top of
plain background) needs waterway lines pre-cropped to the land side, or a
river/stream stroke would visibly double up with the water polygon it
already runs through. See docs/EXAMPLE_PROFILES.md for the full rationale.
"""
from shapely.geometry import GeometryCollection, LineString, MultiLineString, shape

from tilealchemist import water
from tilealchemist.profiles.base import Profile


def _line_components(geometry):
    """Keep only the line parts of a difference() result. Insurance against
    the GEOS version, not something observed on today's: a line-against-
    polygon difference can in principle return a GeometryCollection mixing
    line pieces with degenerate Points where the line merely touches the
    polygon, which older (pre-OverlayNG) GEOS did emit. GEOS 3.11 restricts
    the result to the left operand's dimension instead and never does -
    checked across ~14k real waterway geometries at z13 - but `shapely` is
    deliberately unpinned in profiles/requirements.txt, so the version isn't
    ours to assume. Worth the isinstance check because the failure isn't a
    subtly different encoding: mapbox_vector_tile.encode() raises outright
    ("Encoding geometry collections not supported"), taking down the whole
    shard rather than one feature."""
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
            union = water.snap_to_output_grid(union)

        features = []
        for feature in waterway.features:
            geometry = shape(feature["geometry"])
            if union is not None:
                geometry = geometry.difference(union, grid_size=water.OUTPUT_GRID_SIZE)
            cropped = _line_components(geometry)
            if cropped.is_empty:
                continue
            features.append({"geometry": cropped, "properties": feature["properties"]})

        if not features:
            return None

        return features, extent

    # No gap_tile_bytes override: a gap tile means the source archive had
    # nothing at all for this tile_id (no water, no waterway), so there's no
    # faithful "cropped waterway" content to invent. `Profile`'s default
    # gap_tile_bytes already lands on this: transform_layer({}) sees no
    # waterway layer and returns None, same as any real tile with no
    # waterway features at all.


PROFILE = CroppedWaterwaysProfile