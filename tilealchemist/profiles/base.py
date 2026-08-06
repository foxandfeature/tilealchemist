"""Profile contract: what turns one source tile into one output tile. A
profile owns the per-tile transform, the gap-tile policy, and the output
layer's naming/metadata; it never touches fetching, sharding, or how tiles
reach it (see build_shard.py for that generic machinery).
"""
from abc import ABC, abstractmethod


class Profile(ABC):
    name: str                # CLI value, e.g. "land", "cropped-waterways"
    output_layer_name: str   # output MVT layer name
    mbtiles_name: str        # mbtiles metadata "name" value

    @abstractmethod
    def vector_layers_json(self):
        """The `vector_layers` array embedded in mbtiles metadata's `json`
        field, e.g. [{"id": "land", "fields": {}}]."""

    @abstractmethod
    def transform_tile_bytes(self, data):
        """One gzipped source MVT tile's bytes -> gzipped output MVT bytes,
        or None to skip (write nothing for this tile). Whatever the
        transform actually does (invert, crop, filter, reclassify, ...) is
        entirely up to the implementation; this contract makes no
        assumption about it."""

    @abstractmethod
    def gap_tile_bytes(self, zoom, tile_column, tile_row):
        """Bytes to write for this (z, x, y) gap tile (a tile_id entirely
        absent from the source archive), or None to skip writing this one.
        Called once per gap tile, with that tile's own coordinates, so a
        profile is free to vary its answer by location or zoom (e.g. "no
        gap content above z10") instead of being forced into one fixed
        answer for the whole run. A profile whose content genuinely doesn't
        vary (like `land`'s bare square) should still memoize internally
        rather than recomputing per call, since this can be called a lot:
        large empty stretches (deep desert, ice sheet interiors, ...) can
        be hundreds of thousands of tiles in a single worker."""
