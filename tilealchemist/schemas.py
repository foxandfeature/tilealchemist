"""Tile schema contract: what a schema knows about its own layer structure.

Expressed as behavior a profile calls, never config a profile reads.

A schema answers *feature sets* (see features.py). Each `@feature`-marked
method pulls one named meaning out of a decoded tile's layers, and a profile
MUST ask for one by object, never by layer name. docs/PROFILES.md
("`TileSchema`") has the full contract and the reasoning.
"""
from abc import ABC
from enum import StrEnum

from tilealchemist.features import SURFACE_WATER, WATERWAYS


class SchemaName(StrEnum):
    """Every schema this repo ships, as the name that crosses every boundary.

    What a `Source` declares, what `source.json` carries, what `--schema`
    accepts, what a transform pool worker rebuilds its schema from. A closed
    set rather than a bare string, so a name that is not one of these raises
    ValueError where it is read, not a KeyError deep inside a worker.

    A StrEnum because the two boundaries it crosses are textual: JSON writes
    the member as its plain value, and argparse shows and matches it as one
    (see prepare_shards.py's schema_type()).
    """
    OPENMAPTILES = "openmaptiles"
    PROTOMAPS = "protomaps"


def feature(feature_set, *, fields=None):
    """Marks a `TileSchema` method as answering `feature_set` (see
    features.py).

    Decoded layers in; out, a list of the per-feature {"geometry": ...,
    "properties": ...} dicts `mapbox_vector_tile.decode()` produces, empty if
    this schema has no such data in this tile. Geometry MUST stay the raw
    decoded dict; `Tile.features()` converts it.

    `fields` declares the MVT field types of the properties this feature set
    carries ({"name": "String", ...}), reachable via `schema.fields_for()`."""
    def mark(method):
        method.feature_set = feature_set
        method.feature_fields = fields or {}
        return method
    return mark


def _layer_features(layers, layer_name):
    """`layer_name`'s decoded features, or [] when this tile carries no such
    layer, or an empty one.

    Where every `@feature` method below starts. A schema's layers are
    whatever the tile encoded, so a missing one is ordinary, not an error."""
    layer = layers.get(layer_name)
    if not layer:
        return []
    return layer["features"]


class TileSchema(ABC):
    name: SchemaName             # SCHEMAS key: what a Source declares, what
                                 # source.json carries, what --schema names
    default_buffer_pixels: int   # edge-buffer geometry this schema's tiles carry
    default_extent: int          # MVT extent assumed for a layerless tile
    tile_size_pixels: int = 256
    provides = {}                # {FeatureSet: method name}, filled per subclass

    def __init_subclass__(cls, **kwargs):
        """Collects `provides` ({FeatureSet: method name}) from the
        `@feature`-marked methods. A schema then cannot claim a feature set
        it does not implement, or forget to claim one it does."""
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
        """`feature_set`'s features out of `layers`, raw as decoded. Called
        by `Tile.features()`, which memoizes and converts to shapely."""
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
                f"schema '{self.name}' provides no feature set "
                f"{feature_set.name!r} (it provides: {available})") from None
        return getattr(self, name)


class OpenMapTilesSchema(TileSchema):
    """OpenMapTiles' own layer and attribute naming, matching OpenFreeMap and
    most other OpenMapTiles-schema PMTiles providers.

    One `water` polygon layer, one `waterway` line layer, and
    tunnel/bridge/ford classification in one string attribute, `brunnel`.
    """
    name = SchemaName.OPENMAPTILES
    # Planetiler's own default buffer (`defaultBufferPixels` in
    # FeatureCollector), left unchanged by OpenMapTiles for
    # `water`/`waterway` (`BUFFER_SIZE` in OpenMapTilesSchema.java). Label
    # layers override it much wider (`place` uses 256), hence *default*.
    default_buffer_pixels = 4
    # What every OpenMapTiles/Planetiler-produced tile encodes its layers at.
    # Consulted only for a tile with no layer to read an extent from: a gap
    # tile, or a real tile whose layers all came back empty.
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        return [
            polygon
            for polygon in _layer_features(layers, "water")
            if polygon["properties"].get("brunnel") != "tunnel"
        ]

    # Fields per the OpenMapTiles schema docs (openmaptiles.org/schema/#waterway).
    @feature(WATERWAYS, fields={"class": "String", "name": "String",
                                "brunnel": "String", "intermittent": "Boolean"})
    def waterways(self, layers):
        return _layer_features(layers, "waterway")


# `water` holds polygons, lines and label points in one layer.
# ProtomapsSchema tells them apart by the geometry type
# `mapbox_vector_tile.decode()` writes into each feature's geometry dict.
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
LINE_TYPES = {"LineString", "MultiLineString"}


class ProtomapsSchema(TileSchema):
    """Protomaps' own basemap schema (v4), as its daily planet builds carry
    it. There is no waterway layer at all.

    Water polygons, waterway lines and water label points share one `water`
    layer, told apart by geometry type. Protomaps' own styles read it the
    same way: their water fill layer filters `["==", "$type", "Polygon"]`.
    """
    name = SchemaName.PROTOMAPS
    # The basemap's Water.java/Earth.java call `setBufferPixels(8)` on water
    # polygons and on `earth`; water *lines* keep Planetiler's default 4. The
    # wider of the two is the safe default, because a profile reasoning about
    # tile edges must cover the polygons reaching the full 8. Confirmed
    # against build 20260908 at extent 4096: polygons run to -128..4224,
    # lines to -64..4160.
    default_buffer_pixels = 8
    # As for OpenMapTiles above: what Planetiler encodes at, consulted only
    # for a tile with no layer to read an extent from.
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        """Every polygon in `water`, minus tunnels.

        No `kind` is filtered out — ocean, lake, playa, reef — because
        Protomaps' own style paints them all as water. `tunnel` is encoded
        only from z14 (`extraAttrMinzoom` in Water.java), so below that zoom
        no polygon declares itself one."""
        return [
            polygon
            for polygon in _layer_features(layers, "water")
            if polygon["geometry"]["type"] in POLYGON_TYPES
            and polygon["properties"].get("tunnel", "no") == "no"
        ]

    # What Protomaps sets on a water *line*. The localized `name:<lang>`,
    # `name2` and `script` variants ride along on the features themselves.
    # `kind_detail`, `bridge` and `tunnel` are polygon-only in this basemap.
    @feature(WATERWAYS, fields={"kind": "String", "name": "String",
                                "layer": "Number", "min_zoom": "Number",
                                "sort_rank": "Number"})
    def waterways(self, layers):
        return [line for line in _layer_features(layers, "water")
                if line["geometry"]["type"] in LINE_TYPES]


OPENMAPTILES = OpenMapTilesSchema()
PROTOMAPS = ProtomapsSchema()

# CLI-facing registry, mirroring sources/__init__.py's SOURCES. Another
# schema is one TileSchema subclass instance, one SchemaName member and one
# entry here, with no other code changes. docs/PROFILES.md says why this
# stays a hardcoded dict while profiles are resolved from a path.
SCHEMAS = {schema.name: schema for schema in (OPENMAPTILES, PROTOMAPS)}
