"""Tile schema contract: what a schema knows about its own layer structure,
expressed as behavior a profile calls, not config a profile reads.

A schema answers *feature sets* (see features.py): each `@feature`-marked
method pulls one named meaning out of a decoded tile's layers, and a profile
asks for one by object, never by layer name. See docs/PROFILES.md
("`TileSchema`") for the full contract and the reasoning behind it.
"""
from abc import ABC

from tilealchemist.features import SURFACE_WATER, WATERWAYS


def feature(feature_set, *, fields=None):
    """Marks a `TileSchema` method as answering `feature_set` (a `FeatureSet`,
    see features.py): decoded layers in, a list of the per-feature
    {"geometry": ..., "properties": ...} dicts `mapbox_vector_tile.decode()`
    produces out, empty list if this schema has no such data in this tile.
    Geometry stays the raw decoded dict; `Tile.features()` converts it.

    `fields` declares the MVT field types of the properties this feature set
    carries ({"name": "String", ...}), reachable via `schema.fields_for()`."""
    def mark(method):
        method.feature_set = feature_set
        method.feature_fields = fields or {}
        return method
    return mark


class TileSchema(ABC):
    name: str                    # SCHEMAS key: what a Source declares, what
                                 # source.json carries, what --schema names
    default_buffer_pixels: int   # edge-buffer geometry this schema's tiles carry
    default_extent: int          # MVT extent assumed for a layerless tile
    tile_size_pixels: int = 256
    provides = {}                # {FeatureSet: method name}, filled per subclass

    def __init_subclass__(cls, **kwargs):
        """Collects `provides` ({FeatureSet: method name}) from the
        `@feature`-marked methods, so a schema can't claim a feature set it
        doesn't implement (or forget to claim one it does)."""
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
        """`feature_set`'s features out of `layers`, raw as decoded. Called by
        `Tile.features()`, which memoizes and converts to shapely."""
        return self._method_for(feature_set)(layers)

    def fields_for(self, feature_set):
        """The `fields=` declaration of this schema's `feature_set` method (see
        `feature()`), or {} if it declares none."""
        return self._method_for(feature_set).feature_fields

    def _method_for(self, feature_set):
        """This schema's bound `@feature` method for `feature_set`, or a
        KeyError naming the feature sets it does provide."""
        try:
            name = self.provides[feature_set]
        except KeyError:
            available = ", ".join(sorted(each.name for each in self.provides))
            raise KeyError(
                f"schema {self.name!r} provides no feature set "
                f"{feature_set.name!r} (it provides: {available})") from None
        return getattr(self, name)


class OpenMapTilesSchema(TileSchema):
    """OpenMapTiles' own layer/attribute naming, matching OpenFreeMap and most
    other OpenMapTiles-schema PMTiles providers: a single `water` polygon layer
    and a single `waterway` line layer, tunnel/bridge/ford classification via
    one string attribute (`brunnel`).
    """
    name = "openmaptiles"
    # Planetiler's own default buffer (`defaultBufferPixels` in
    # FeatureCollector), left unchanged by OpenMapTiles for `water`/`waterway`
    # (`BUFFER_SIZE` in OpenMapTilesSchema.java). Label layers override it much
    # wider (`place` uses 256), which is why this is a *default*
    default_buffer_pixels = 4
    # What every OpenMapTiles/Planetiler-produced tile encodes its layers at.
    # Only ever consulted for a tile with no layer to read an extent from: a
    # gap tile, or a real tile whose layers all came back empty.
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        water = layers.get("water")
        if not water:
            return []
        return [
            polygon
            for polygon in water["features"]
            if polygon["properties"].get("brunnel") != "tunnel"
        ]

    # Fields per the OpenMapTiles schema docs (openmaptiles.org/schema/#waterway).
    @feature(WATERWAYS, fields={"class": "String", "name": "String",
                                "brunnel": "String", "intermittent": "Boolean"})
    def waterways(self, layers):
        waterway = layers.get("waterway")
        if not waterway:
            return []
        return waterway["features"]


# `water` holds polygons, lines and label points in one layer, so
# ProtomapsSchema tells them apart by the geometry type
# `mapbox_vector_tile.decode()` writes into each feature's geometry dict.
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
LINE_TYPES = {"LineString", "MultiLineString"}


class ProtomapsSchema(TileSchema):
    """Protomaps' own basemap schema (v4), as its daily planet builds carry
    it: no waterway layer at all. Water polygons, waterway lines and water
    label points share one `water` layer, told apart by geometry type, which
    is how Protomaps' own styles read it too (their water fill layer filters
    `["==", "$type", "Polygon"]`).
    """
    name = "protomaps"
    # The basemap's Water.java/Earth.java call `setBufferPixels(8)` on water
    # polygons and on `earth`; water *lines* keep Planetiler's default 4. The
    # wider of the two is the safe default: a profile reasoning about tile
    # edges has to cover the polygons that reach the full 8. Confirmed against
    # build 20260908 at extent 4096: polygons run to -128..4224, lines to
    # -64..4160.
    default_buffer_pixels = 8
    # As for OpenMapTiles above: what Planetiler encodes at, consulted only for
    # a tile with no layer to read an extent from.
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        """Every polygon in `water`, minus tunnels. No `kind` is filtered out
        (ocean, lake, playa, reef...): Protomaps' own style paints them all as
        water. `tunnel` is only encoded from z14 (`extraAttrMinzoom` in
        Water.java), so below that zoom no polygon declares itself one."""
        water = layers.get("water")
        if not water:
            return []
        return [
            polygon
            for polygon in water["features"]
            if polygon["geometry"]["type"] in POLYGON_TYPES
            and polygon["properties"].get("tunnel", "no") == "no"
        ]

    # What Protomaps sets on a water *line*. The localized `name:<lang>`,
    # `name2` and `script` variants ride along on the features themselves;
    # `kind_detail`, `bridge` and `tunnel` are polygon-only in this basemap.
    @feature(WATERWAYS, fields={"kind": "String", "name": "String",
                                "layer": "Number", "min_zoom": "Number",
                                "sort_rank": "Number"})
    def waterways(self, layers):
        water = layers.get("water")
        if not water:
            return []
        return [line for line in water["features"]
                if line["geometry"]["type"] in LINE_TYPES]


OPENMAPTILES = OpenMapTilesSchema()
PROTOMAPS = ProtomapsSchema()

# CLI-facing registry, mirroring sources/__init__.py's SOURCES: another schema
# is its own TileSchema subclass instance plus one entry here, no other code
# changes. docs/PROFILES.md says why this stays a hardcoded dict while profiles
# are resolved from a path instead.
SCHEMAS = {schema.name: schema for schema in (OPENMAPTILES, PROTOMAPS)}
