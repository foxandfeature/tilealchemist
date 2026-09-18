"""Profile contract: what turns one source tile into one output tile.

A profile MUST implement `name` and `transform()`. Everything else here has
a working default. A profile is constructed with no arguments and MUST hold
no schema or per-run state. See docs/PROFILES.md for the full contract, the
reasoning behind it, and how to write one of your own.
"""
from abc import ABC, abstractmethod

from shapely.geometry.base import BaseGeometry

from tilealchemist import mvt
from tilealchemist.features import Feature
from tilealchemist.tile import Tile


class Profile(ABC):
    name: str    # identifier for logging and, by default, for the output layer
                 # and mbtiles metadata name

    @property
    def output_layer_name(self):
        """Output MVT layer name; `name` unless a profile overrides this."""
        return self.name

    @property
    def mbtiles_name(self):
        """mbtiles metadata "name" value; `name` unless overridden."""
        return self.name

    @abstractmethod
    def transform(self, tile):
        """This profile's output for one `Tile` (see tile.py).

        A `Feature`, a shapely geometry, an iterable of either mixed freely,
        or None/an empty result to skip the tile entirely."""

    def output_fields(self, schema):
        """Field name -> MVT field type for the properties this profile's own
        output features carry, for `vector_layers_json()`."""
        return {}

    def vector_layers_json(self, schema):
        """The `vector_layers` array embedded in mbtiles metadata's `json`
        field. The default describes one output layer."""
        return [{"id": self.output_layer_name,
                 "fields": self.output_fields(schema)}]

    def transform_tile(self, tile):
        """One `Tile` -> gzipped output MVT bytes, or None to skip.

        Not sealed. A profile needing non-MVT output, or full control over
        encoding, MAY override this directly."""
        result = self.transform(tile)
        if not result:
            return None
        if isinstance(result, (Feature, BaseGeometry)):
            result = [result]
        features = [item if isinstance(item, Feature) else Feature(item)
                    for item in result]
        return self._encode_tile(features, tile.extent)

    def transform_gap(self, schema):
        """Bytes to write at every gap tile — a tile_id entirely absent from
        the source archive — or None to write nothing there. Called once per
        run, not once per tile: a gap tile is a tile with no layers."""
        return self.transform_tile(Tile.empty(schema))

    def _encode_tile(self, features, extent):
        """This profile's `Feature`s -> gzipped MVT bytes under its own
        `output_layer_name`, or None if nothing survives.

        The one place a profile touches the codec directly, and where
        `Feature` ends and mapbox_vector_tile's dict format begins."""
        return mvt.encode_tile(
            self.output_layer_name,
            [{"geometry": feature.geometry, "properties": feature.properties}
             for feature in features],
            extent)
