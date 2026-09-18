"""One source tile as a profile sees it.

Its decoded layers, the schema that explains them, and a place for derived
values to be computed once and shared. transform.py builds one `Tile` per
source tile and hands it to every profile in the run, so the decode and any
expensive intermediate (a water union, say) happens once per tile rather
than once per profile. See docs/PROFILES.md.

A `Tile` MUST carry no coordinate. Everything on it is tile-local, hence
identical for every z/x/y that dedupes to the same bytes. Three things rest
on that invariant: one object standing for a whole run_length run; one
standing for two entries pointing at the same (offset, length), which
offset-ordered batching makes adjacent (both in
transform_batch_blob_multi()); and one `Tile.empty()` serving a
hundred-thousand-tile gap region (transform_gap() in profiles/base.py).
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
        """A tile with no layers: every feature set comes back empty, and
        `extent` falls back to the schema's `default_extent`. Both a gap tile
        (a tile_id the archive has no data for) and a real tile whose layers
        all came back empty amount to this one."""
        return cls({}, schema)

    @cached_property
    def extent(self):
        """This tile's MVT coordinate extent.

        MVT allows one per layer, but a tile in practice encodes every layer
        at the same extent, so the first layer's is the tile's. A tile with
        no layers falls back to what the schema says its producer encodes
        at."""
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
        """This tile's `Feature`s for `feature_set` (see features.py), empty
        if it has no such data. Raises KeyError naming what the schema does
        provide if it cannot answer this one. Computed once per tile, however
        many profiles ask."""
        return self.derived("features", feature_set, lambda tile: [
            Feature(shape(feature["geometry"]), feature["properties"])
            for feature in tile.schema.extract(feature_set, tile.layers)
        ])

    def derived(self, namespace, key, compute):
        """`compute(self)`, memoized on this tile under `namespace`/`key`.

        The general form of what `features()` does. A profile, or a shared
        helper like water.py, hangs its expensive per-tile intermediate here.
        The second profile to read the same tile gets it back instead of
        repeating it.

        `namespace` is the helper owning the value, `key` what it calls this
        one. Both are any hashable, usually a module name and a string.
        Splitting them keeps two helpers that both memoize a "union" apart
        without either having to prefix it."""
        memo_key = (namespace, key)
        if memo_key not in self._derived:
            self._derived[memo_key] = compute(self)
        return self._derived[memo_key]
