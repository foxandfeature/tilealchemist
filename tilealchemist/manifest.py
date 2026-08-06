"""Shared binary manifest format for handing PMTiles directory entries from
the one-time `prepare_shards.py` walk to each `build_shard.py`
worker, without re-walking the archive's directory tree per worker.

One manifest file per worker, a flat sequence of fixed-size records. No
framing is needed, since file size / RECORD.size gives the count:

    tile_id: uint64, offset: uint64, length: uint32, run_length: uint32

These mirror a PMTiles directory entry: `tile_id` is the Hilbert-curve
index of the tile, `offset`/`length` locate its bytes in the archive's
tile data section, and `run_length` is how many consecutive tile_ids
from `tile_id` share those same bytes. See README.md's "Fetching"
section for why entries are stored in ascending offset order and what
`run_length` is used for.
"""
import struct
from collections import namedtuple

RECORD = struct.Struct("<QQII")  # tile_id, offset, length, run_length

Entry = namedtuple("Entry", ["tile_id", "offset", "length", "run_length"])


def write_manifest(path, entries):
    with open(path, "wb") as file:
        for entry in entries:
            file.write(RECORD.pack(entry.tile_id, entry.offset, entry.length, entry.run_length))


def read_manifest(path):
    with open(path, "rb") as file:
        data = file.read()
    return [
        Entry(*RECORD.unpack_from(data, offset))
        for offset in range(0, len(data), RECORD.size)
    ]
