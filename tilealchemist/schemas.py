"""Tile schema contract: what a schema knows about its own layer structure,
expressed as behavior a profile calls (like `Source`/`Profile`, see
sources/base.py and profiles/base.py), not config a profile reads and
interprets itself. `surface_water()`/`waterway_lines()` exist because "get
real, non-tunnel surface water out of this tile" can differ structurally
between schemas, not just by naming: OpenMapTiles keeps all water in one
polygon layer with a single string tunnel/bridge/ford attribute, while
Shortbread splits water across two layers (`ocean` and `water_polygons`)
and uses separate boolean `tunnel`/`bridge` fields with no tunnel concept
on polygons at all (checked against Shortbread's real 1.0 schema docs, not
assumed). A generic name-to-name mapping can't express "union these two
layers" or "this schema uses a boolean here, not a string enum": there's
nothing generic left to factor out once a schema's own logic is what
decides the answer, so each `TileSchema` subclass just implements it
directly, the same way each `Profile` implements `transform_tile_bytes()`
directly.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SurfaceWater:
    extent: int
    polygons: list  # raw MVT-decoded geometry dicts (e.g. {"type": "Polygon",
                     # "coordinates": [...]}, exactly what mapbox_vector_tile.decode()
                     # produces per-feature); tunnel/bridge water already excluded, per
                     # this schema's own rules. Geometry-library-agnostic by design:
                     # parsing into shapely or anything else is up to the consuming profile.


@dataclass(frozen=True)
class WaterwayFeatures:
    extent: int
    features: list  # [{"geometry": <raw MVT-decoded geometry dict>, "properties": dict}, ...],
                     # properties preserved as-is; geometry left unparsed, same as
                     # SurfaceWater.polygons


class TileSchema(ABC):
    default_buffer_pixels: int
    tile_size_pixels: int = 256

    @abstractmethod
    def surface_water(self, decoded_tile):
        """Real, non-tunnel surface water polygons in this tile, as a
        SurfaceWater(extent, polygons), or None if there's no water data at
        all for this schema."""

    @abstractmethod
    def waterway_lines(self, decoded_tile):
        """This tile's waterway line features, geometry left as the raw
        MVT-decoded dict (not parsed into any geometry library) and
        properties preserved as-is, as a WaterwayFeatures(extent, features),
        or None if there's no waterway data at all for this schema."""

    @abstractmethod
    def waterway_fields(self):
        """Field name -> MVT field type ("String"/"Number"/"Boolean") for
        this schema's waterway line features, for a profile to embed in its
        `vector_layers_json()`. Schema-specific for the same reason
        `waterway_lines()` is: field names and types aren't guaranteed to
        match across schemas."""


class OpenMapTilesSchema(TileSchema):
    """OpenMapTiles' own layer/attribute naming, matching OpenFreeMap and
    most other OpenMapTiles-schema PMTiles providers: a single `water`
    polygon layer and a single `waterway` line layer, tunnel/bridge/ford
    classification via one string attribute (`brunnel`, "tunnel" meaning
    excluded from surface water). `default_buffer_pixels=4` is
    OpenMapTiles/Planetiler's own default buffer for the `water` layer
    (`BUFFER_SIZE` in `OpenMapTilesSchema.java`, `defaultBufferPixels` in
    `FeatureCollector`), not an invention of any one profile here.
    """
    default_buffer_pixels = 4

    def surface_water(self, decoded_tile):
        water = decoded_tile.get("water")
        if not water:
            return None
        polygons = [
            feature["geometry"]
            for feature in water["features"]
            if feature["properties"].get("brunnel") != "tunnel"
        ]
        return SurfaceWater(extent=water["extent"], polygons=polygons)

    def waterway_lines(self, decoded_tile):
        waterway = decoded_tile.get("waterway")
        if not waterway:
            return None
        features = [
            {"geometry": feature["geometry"], "properties": feature["properties"]}
            for feature in waterway["features"]
        ]
        return WaterwayFeatures(extent=waterway["extent"], features=features)

    def waterway_fields(self):
        # Per the OpenMapTiles schema docs (openmaptiles.org/schema/#waterway).
        return {"class": "String", "name": "String", "brunnel": "String", "intermittent": "Boolean"}


OPENMAPTILES = OpenMapTilesSchema()

# CLI-facing registry, mirroring sources/__init__.py's SOURCES (profiles
# have no equivalent dict: see profiles/__init__.py's load_profile(), which
# resolves every profile, built-in or not, straight from its .py path
# instead): a second schema is added here as its own TileSchema subclass
# instance plus one entry, no other code changes needed.
SCHEMAS = {"openmaptiles": OPENMAPTILES}
