"""The closed set of zoom levels this pipeline can walk.

A zoom MUST be a `ZoomLevel` member everywhere the pipeline passes one
around. `--min-zoom`/`--max-zoom` parse into one, `source.json` reads back
into one. A level outside the set raises ValueError where it enters, rather
than walking to nothing.

`IntEnum` because a zoom level *is* a number wherever the pipeline computes
with it: `zxy_to_tileid(max_zoom + 1, ...)` in pmtiles_index.py, the
`min_zoom <= zoom <= max_zoom` filter in transform.py, the `2 ** zoom` row
flip in mbtiles.py. Members compare, format, pickle and serialize as the
plain ints they replace.
"""
from enum import IntEnum

# zxy_to_tileid() raises OverflowError above z=31: tile_id stops fitting a
# 64-bit int. tile_id_bounds() always asks it for max_zoom + 1, so max_zoom
# MUST stay at 30 or below.
MAX_SUPPORTED_ZOOM = 30

# Generated so the limit above stays the single fact deciding where the set
# ends. `module`/`qualname` make members of a functional-API enum picklable,
# which transform.py's ChunkJob needs to reach a pool worker process.
ZoomLevel = IntEnum(
    "ZoomLevel",
    {f"Z{level}": level for level in range(MAX_SUPPORTED_ZOOM + 1)},
    module=__name__,
    qualname="ZoomLevel",
)
