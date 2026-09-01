"""Profile contract: what turns one source tile into one output tile. A
profile owns the per-tile transform, the gap-tile policy, and the output
layer's naming/metadata; it never touches fetching, sharding, or how tiles
reach it (see build_shard.py for that generic machinery). The MVT gzip+
protobuf codec itself isn't a profile's job either: `transform_tile_bytes()`
below is a concrete method that handles decode/encode generically via
`tilealchemist/mvt.py`, so a profile only implements `transform_layer()`
(decoded tile in, this profile's own output layer content out).
"""
from abc import ABC, abstractmethod

from tilealchemist import mvt


class Profile(ABC):
    name: str                # human-readable identifier for logging, e.g. "land",
                              # "cropped-waterways" (a custom profile's own choice;
                              # not necessarily equal to the --profile value that
                              # resolved it, e.g. a file path)
    output_layer_name: str   # output MVT layer name
    mbtiles_name: str        # mbtiles metadata "name" value
    compatible_schemas = None  # frozenset of schemas.SCHEMAS keys this profile
                                # requires, or None if it only calls TileSchema's
                                # universal abstract API and works with any schema

    @abstractmethod
    def vector_layers_json(self):
        """The `vector_layers` array embedded in mbtiles metadata's `json`
        field, e.g. [{"id": "land", "fields": {}}]."""

    def transform_tile_bytes(self, data):
        """One gzipped source MVT tile's bytes -> gzipped output MVT bytes,
        or None to skip (write nothing for this tile). Generic: decodes via
        `mvt.decode_tile()`, hands the decoded tile to `transform_layer()`,
        and encodes whatever comes back via `_encode_tile()`. Not sealed: a
        profile with unusual needs (non-MVT output, full control over
        encoding) can still override this directly instead of implementing
        `transform_layer()`: it just needs some `transform_layer()`
        (even a stub) to satisfy this ABC."""
        decoded = mvt.decode_tile(data)
        result = self.transform_layer(decoded)
        if result is None:
            return None
        features, extent = result
        return self._encode_tile(features, extent)

    @abstractmethod
    def transform_layer(self, decoded_tile):
        """decoded_tile (the {layer_name: {...}} dict from mvt.decode_tile())
        -> (features, extent) for this profile's own output layer, or None
        to skip (write nothing for this tile). `features` is a list of
        {"geometry": <a geometry object mapbox_vector_tile.encode() accepts>,
        "properties": dict}: which geometry library produces that object
        (shapely or anything else) is entirely this profile's own choice,
        not this method's contract. Whatever the transform actually does
        (invert, crop, filter, reclassify, ...) is up to the implementation."""

    def gap_tile_bytes(self, zoom, tile_column, tile_row):
        """Bytes to write for this (z, x, y) gap tile (a tile_id entirely
        absent from the source archive), or None to skip writing this one.
        Default: a gap tile is just an ordinary tile with no source layers
        at all, so this runs it through the same `transform_layer()` a real
        tile would use, with `decoded_tile={}` standing in for "nothing
        here" (every `TileSchema` accessor already treats a missing key as
        "no data", e.g. `decoded_tile.get("water")`). The result is memoized
        after the first call, since (zoom, tile_column, tile_row) go unused
        here and this can be called a lot: large empty stretches (deep
        desert, ice sheet interiors, ...) can be hundreds of thousands of
        tiles in a single worker. Not sealed: a profile that needs a
        different answer, or one that varies by location or zoom (e.g. "no
        gap content above z10"), can override this directly instead and use
        (zoom, tile_column, tile_row)."""
        if not hasattr(self, "_gap_tile_cache"):
            result = self.transform_layer({})
            self._gap_tile_cache = self._encode_tile(*result) if result is not None else None
        return self._gap_tile_cache

    def _encode_tile(self, features, extent):
        return mvt.encode_tile(self.output_layer_name, features, extent)
