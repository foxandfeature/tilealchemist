"""One source tile as a profile sees it: its decoded layers, the schema that
explains them, and a place for anything derived from them to be computed once
and shared.

transform.py builds one `Tile` per source tile and hands that same object to
every profile in the run, so the decode (and any expensive intermediate two
profiles both want, like the water union) happens once per tile rather than
once per profile. See docs/PROFILES.md for the full rationale.

A `Tile` deliberately carries no coordinate: everything on it is tile-local, and
so identical for every z/x/y that dedupes to the same bytes. That is the
invariant that lets one object stand for a whole run_length run, for two
separate entries that point at the same (offset, length), i.e. distinct
tile_ids the source stores once, which offset-ordered batching makes adjacent
(both cases in transform_batch_blob_multi() in transform.py), and for a whole
gap region (one `Tile.empty()` serves a hundred-thousand-tile desert, see
transform_gap() in profiles/base.py). Adding a coordinate here silently breaks
all three.
"""
from functools import cached_property

from shapely.geometry import box, shape

from tilealchemist import mvt
from tilealchemist.features import Feature


class Tile:
    def __init__(self, layers, schema):
        self.layers = layers  # decode_tile()'s {layer_name: {...}} dict
        self.schema = schema
        self._derived = {}

    @classmethod
    def decode(cls, data, schema):
        """One gzipped source MVT tile's bytes -> a Tile."""
        return cls(mvt.decode_tile(data), schema)

    @classmethod
    def empty(cls, schema):
        """A tile with no layers at all: every feature set comes back empty and
        `extent` falls back to the schema's `default_extent`. Both a gap tile (a
        tile_id the source archive has no data for, see shard_worker.py's gap
        entries) and a real tile whose layers all came back empty amount to this
        same tile."""
        return cls({}, schema)

    @cached_property
    def extent(self):
        """This tile's MVT coordinate extent. MVT allows one per layer, but a
        tile in practice encodes every layer at the same extent, so the first
        layer's is the tile's. A tile with no layers has none to read, and
        falls back to what the schema says its producer encodes at."""
        if not self.layers:
            return self.schema.default_extent
        return next(iter(self.layers.values()))["extent"]

    @cached_property
    def buffered_square(self):
        """This tile's own square, buffered past its edge by the schema's
        edge-buffer amount (pixels -> units)."""
        buffer = self.extent * self.schema.default_buffer_pixels / self.schema.tile_size_pixels
        return box(-buffer, -buffer, self.extent + buffer, self.extent + buffer)

    def features(self, feature_set):
        """This tile's `Feature`s for `feature_set` (a `FeatureSet`, see
        features.py), empty if this tile has no such data. Raises KeyError
        naming what the schema does provide if it can't answer this one.
        Computed once per tile, however many profiles ask."""
        return self.derived("features", feature_set, lambda tile: [
            Feature(shape(feature["geometry"]), feature["properties"])
            for feature in tile.schema.extract(feature_set, tile.layers)
        ])

    def derived(self, namespace, key, compute):
        """`compute(self)`, memoized on this tile under `namespace`/`key`.

        The general form of what `features()` does. A profile, or a shared
        helper like water.py, hangs its own expensive per-tile intermediate
        here, and the second profile to read the same tile gets it back
        instead of repeating it.

        `namespace` is the helper that owns the value, `key` what it calls
        this one, both any hashable, usually the module name and a string.
        Splitting them is what keeps two helpers that both memoize a "union"
        apart, without either having to remember to prefix it."""
        memo_key = (namespace, key)
        if memo_key not in self._derived:
            self._derived[memo_key] = compute(self)
        return self._derived[memo_key]
