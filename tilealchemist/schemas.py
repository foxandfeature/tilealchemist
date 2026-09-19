"""Tile schema contract, as behavior a profile calls; see docs/PROFILES.md."""
from abc import ABC
from enum import StrEnum

from tilealchemist.features import SURFACE_WATER, WATERWAYS


class SchemaName(StrEnum):
    OPENMAPTILES = "openmaptiles"
    PROTOMAPS = "protomaps"


def feature(feature_set, *, fields=None):
    """Marks a `TileSchema` method as answering `feature_set`, in raw decoded form."""
    def mark(method):
        method.feature_set = feature_set
        method.feature_fields = fields or {}
        return method
    return mark


def _layer_features(layers, layer_name):
    layer = layers.get(layer_name)
    if not layer:
        return []
    return layer["features"]


class TileSchema(ABC):
    name: SchemaName
    default_buffer_pixels: int
    default_extent: int
    tile_size_pixels: int = 256
    provides = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.provides = {
            method.feature_set: attribute_name
            for attribute_name in dir(cls)
            if (method := getattr(cls, attribute_name, None)) is not None
            and hasattr(method, "feature_set")
        }

    def __repr__(self):
        return f"<TileSchema {self.name}>"

    def extract(self, feature_set, layers):
        return self._method_for(feature_set)(layers)

    def fields_for(self, feature_set):
        return self._method_for(feature_set).feature_fields

    def _method_for(self, feature_set):
        try:
            name = self.provides[feature_set]
        except KeyError:
            available = ", ".join(sorted(each.name for each in self.provides))
            raise KeyError(
                f"schema '{self.name}' provides no feature set "
                f"{feature_set.name!r} (it provides: {available})") from None
        return getattr(self, name)


class OpenMapTilesSchema(TileSchema):
    name = SchemaName.OPENMAPTILES
    # Planetiler's default, which OpenMapTiles leaves alone for water/waterway.
    default_buffer_pixels = 4
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        return [
            polygon
            for polygon in _layer_features(layers, "water")
            if polygon["properties"].get("brunnel") != "tunnel"
        ]

    @feature(WATERWAYS, fields={"class": "String", "name": "String",
                                "brunnel": "String", "intermittent": "Boolean"})
    def waterways(self, layers):
        return _layer_features(layers, "waterway")


# Protomaps puts polygons, lines and label points in one `water` layer.
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
LINE_TYPES = {"LineString", "MultiLineString"}


class ProtomapsSchema(TileSchema):
    name = SchemaName.PROTOMAPS
    # The wider of the schema's two, since water polygons reach the full 8.
    default_buffer_pixels = 8
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        return [
            polygon
            for polygon in _layer_features(layers, "water")
            if polygon["geometry"]["type"] in POLYGON_TYPES
            and polygon["properties"].get("tunnel", "no") == "no"
        ]

    @feature(WATERWAYS, fields={"kind": "String", "name": "String",
                                "layer": "Number", "min_zoom": "Number",
                                "sort_rank": "Number"})
    def waterways(self, layers):
        return [line for line in _layer_features(layers, "water")
                if line["geometry"]["type"] in LINE_TYPES]


OPENMAPTILES = OpenMapTilesSchema()
PROTOMAPS = ProtomapsSchema()

SCHEMAS = {schema.name: schema for schema in (OPENMAPTILES, PROTOMAPS)}
