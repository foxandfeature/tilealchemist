"""Which zoom levels this pipeline can walk, as the closed set they are.

A zoom is one of a fixed list of levels, not an arbitrary number, so it is a
`ZoomLevel` member everywhere the pipeline passes one around:
`--min-zoom`/`--max-zoom` parse into one, `source.json` reads back into one,
and a level outside the set is a ValueError where it enters rather than a walk
that quietly finds nothing.

`ZoomLevel` is an `IntEnum` because a zoom level *is* a number wherever the
pipeline computes with it: `zxy_to_tileid(max_zoom + 1, ...)` in
pmtiles_index.py, the `min_zoom <= zoom <= max_zoom` filter in transform.py,
the `2 ** zoom` row flip and the `minzoom`/`maxzoom` metadata in mbtiles.py.
Members compare, format, pickle and serialize exactly as the plain ints they
replace, so naming the set costs that arithmetic nothing.
"""
from enum import IntEnum

# zxy_to_tileid() raises OverflowError above z=31 (tile_id stops fitting a
# 64-bit int), and pmtiles_index.py's tile_id_bounds() always asks it for
# max_zoom + 1, so max_zoom itself must stay at 30 or below.
MAX_SUPPORTED_ZOOM = 30

# Generated, not spelled out: the levels are exactly 0..MAX_SUPPORTED_ZOOM, and
# the limit above is the single fact that decides where the set ends, so 31
# hand-written members would only be a second place to keep in step with it.
# `module`/`qualname` are what make members of a functional-API enum picklable,
# which transform.py's ChunkJob needs to reach a pool worker process.
ZoomLevel = IntEnum(
    "ZoomLevel",
    {f"Z{level}": level for level in range(MAX_SUPPORTED_ZOOM + 1)},
    module=__name__,
    qualname="ZoomLevel",
)
