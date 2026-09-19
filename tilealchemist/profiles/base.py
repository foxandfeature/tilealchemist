"""Profile contract: one source tile -> one output tile; see docs/PROFILES.md."""
from abc import ABC, abstractmethod

from shapely.geometry.base import BaseGeometry

from tilealchemist import mvt
from tilealchemist.features import Feature
from tilealchemist.tile import Tile


class Profile(ABC):
    name: str

    @property
    def output_layer_name(self):
        return self.name

    @property
    def mbtiles_name(self):
        return self.name

    @abstractmethod
    def transform(self, tile):
        """A Feature, a geometry, an iterable of either, or None to skip the tile."""

    def output_fields(self, schema):
        return {}

    def vector_layers_json(self, schema):
        return [{"id": self.output_layer_name,
                 "fields": self.output_fields(schema)}]

    def transform_tile(self, tile):
        """One `Tile` -> gzipped output MVT bytes, or None to skip; overridable."""
        result = self.transform(tile)
        if not result:
            return None
        if isinstance(result, (Feature, BaseGeometry)):
            result = [result]
        features = [item if isinstance(item, Feature) else Feature(item)
                    for item in result]
        return self._encode_tile(features, tile.extent)

    def transform_gap(self, schema):
        """Bytes for every gap tile, or None; called once per run, not once per tile."""
        return self.transform_tile(Tile.empty(schema))

    def _encode_tile(self, features, extent):
        return mvt.encode_tile(
            self.output_layer_name,
            [{"geometry": feature.geometry, "properties": feature.properties}
             for feature in features],
            extent)
