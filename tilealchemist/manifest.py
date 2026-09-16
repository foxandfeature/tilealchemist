"""Everything `prepare_shards.py` hands to the `build_shard.py` workers, and
the only thing they share: one binary manifest per worker, plus the one
`source.json` all of them read. Both sides of both files live here so the
writer and the reader can't drift apart.

The manifest carries the PMTiles directory entries from the one-time
`prepare_shards.py` walk, so no worker has to re-walk the archive's
directory tree. One file per worker, a flat sequence of fixed-size records.
No framing is needed, since file size / RECORD.size gives the count:

    tile_id: uint64, offset: uint64, length: uint32, run_length: uint32

These mirror a PMTiles directory entry: `tile_id` is the Hilbert-curve
index of the tile, `offset`/`length` locate its bytes in the archive's
tile data section, and `run_length` is how many consecutive tile_ids
from `tile_id` share those same bytes. See README.md's "Fetching"
section for why entries are stored in ascending offset order and what
`run_length` is used for.

`source.json` carries what is true for the whole run rather than for one
worker: which archive to fetch from, which schema its tiles are in, the zoom
bounds it was walked at, and the tile-data base offset every manifest offset
is relative to. The schema rides along here for the same reason the URL does:
both are what `prepare_shards.py` resolved, not a worker's own opinion. It
goes out as plain JSON and comes back as a `SourceMetadata`, whose fields are
the `SchemaName` and `ZoomLevel` members it was written from, so a
hand-edited file is caught here rather than deep in a run.
"""
import json
import os
import struct
from collections import namedtuple
from dataclasses import dataclass

from tilealchemist.schemas import SchemaName
from tilealchemist.zoom import ZoomLevel

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


def write_worker_manifests(out_dir, blocks):
    """One manifest per block, named by worker index: the file each worker is
    pointed at by `--manifest` (see `.github/workflows/_pipeline.yml`). An
    empty block still gets its file, so worker N always has one to read."""
    for worker_index, block in enumerate(blocks):
        write_manifest(os.path.join(out_dir, f"worker-{worker_index:03d}.bin"), block)


@dataclass(frozen=True)
class SourceMetadata:
    """What `source.json` says about the run as a whole, as the workers hold
    it: `prepare_shards.py`'s resolved archive, the schema its tiles are in,
    the zoom bounds it was walked at, and the offset manifest offsets are
    relative to. The `ResolvedSource` half of this (see sources/base.py) plus
    what the walk itself decided.

    The JSON key strings live in `as_json()`/`from_json()` below and nowhere
    else: every reader of a run's metadata names a field, so a renamed or
    missing one is a mistake at the two ends of the file format rather than a
    KeyError anywhere a worker happens to look something up.
    """

    url: str                 # pmtiles URL to range-GET against
    build: str               # human-readable label for logs ("n/a" if not applicable)
    schema: SchemaName       # which schema the archive's tiles are in
    min_zoom: ZoomLevel      # the bounds the walk was pruned to, which the
    max_zoom: ZoomLevel      # transform filters tiles of a run against again
    tile_data_offset: int    # where the archive's tile data section starts

    def as_json(self):
        """Plain strings and numbers: `source.json` is read by other tools
        than this one (and by a human), so it carries exactly what `--schema`
        and `--min-zoom`/`--max-zoom` themselves take."""
        return {
            "url": self.url,
            "build": self.build,
            "schema": self.schema.value,
            "min_zoom": self.min_zoom.value,
            "max_zoom": self.max_zoom.value,
            "tile_data_offset": self.tile_data_offset,
        }

    @classmethod
    def from_json(cls, document):
        """Back to the members as_json() wrote them from, so a source.json
        naming a schema this build doesn't have, or a zoom level outside the
        range the walk supports, fails here, with the offending value in the
        message, rather than somewhere inside a worker."""
        return cls(
            url=document["url"],
            build=document["build"],
            schema=SchemaName(document["schema"]),
            min_zoom=ZoomLevel(document["min_zoom"]),
            max_zoom=ZoomLevel(document["max_zoom"]),
            tile_data_offset=document["tile_data_offset"],
        )


def write_source_metadata(out_dir, resolved_source, min_zoom, max_zoom, tile_data_offset):
    metadata = SourceMetadata(
        url=resolved_source.url,
        build=resolved_source.build,
        schema=resolved_source.schema.name,
        min_zoom=ZoomLevel(min_zoom),
        max_zoom=ZoomLevel(max_zoom),
        tile_data_offset=tile_data_offset,
    )
    with open(os.path.join(out_dir, "source.json"), "w") as source_file:
        json.dump(metadata.as_json(), source_file)


def read_source_metadata(path):
    """The `SourceMetadata` every worker starts from (see shard_worker.py)."""
    with open(path) as source_file:
        return SourceMetadata.from_json(json.load(source_file))
