"""The mbtiles side of a worker: one shard database per profile, and the
writes that fill it.

Apart from the transform (`transform.py`) and the worker's own control flow
(`shard_worker.py`) because it is the only part that knows sqlite and the
mbtiles convention, notably that mbtiles numbers rows TMS-style, so every
write flips the row (`(2 ** zoom - 1) - tile_row`) out of the XYZ numbering
a PMTiles tile-ID decodes to. A shard is a plain mbtiles file rather than
the finished .pmtiles because the pipeline's last job merges every worker's
shards with `tile-join` (see docs/ARCHITECTURE.md "Parallelism")."""
import json
import sqlite3
import sys

from pmtiles.tile import tileid_to_zxy


class ProfileTileCounts:
    """One profile's running written/skipped totals, accumulated across the
    real-entry and gap-entry phases."""

    def __init__(self):
        self.written = 0
        self.skipped = 0

    def add(self, written, skipped):
        self.written += written
        self.skipped += skipped


def init_mbtiles(path, min_zoom, max_zoom, profile, schema):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    connection.execute(
        "CREATE TABLE tiles ("
        "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    connection.execute(
        "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    vector_layers_json = json.dumps(
        {"vector_layers": profile.vector_layers_json(schema)}, separators=(",", ":"))
    connection.executemany(
        "INSERT INTO metadata (name, value) VALUES (?, ?)",
        [
            ("name", profile.mbtiles_name),
            ("format", "pbf"),
            ("minzoom", str(min_zoom)),
            ("maxzoom", str(max_zoom)),
            ("json", vector_layers_json),
        ],
    )
    connection.commit()
    return connection


def write_output_tiles(results, connection):
    written = skipped = 0
    for zoom, tile_column, tile_row, output_data in results:
        if output_data is None:
            skipped += 1
            continue
        tms_row = (2 ** zoom - 1) - tile_row
        connection.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            (zoom, tile_column, tms_row, output_data),
        )
        written += 1
    return written, skipped


def write_gap_tiles(gap_entries, connection, output_data):
    """Writes `output_data` at every gap tile's coordinates, or nothing at
    all when it is None. That single value is the profile's whole answer for
    every gap tile in the run (`transform_gap()`, see profiles/base.py);
    shard_worker.py asks for it, the same way it hands run_transform() the
    real entries' bytes, so that transforming stays out of this module.

    `tms_rows()` is a generator so a worker holding a million-tile gap (an
    ocean, a whole desert or ice sheet interior) never materializes them all
    as one Python list before handing them to sqlite3."""
    total = sum(entry.run_length for entry in gap_entries)

    def tms_rows():
        for entry in gap_entries:
            for run_offset in range(entry.run_length):
                zoom, tile_column, tile_row = tileid_to_zxy(entry.tile_id + run_offset)
                yield (zoom, tile_column, (2 ** zoom - 1) - tile_row, output_data)

    if output_data is not None:
        connection.executemany(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            tms_rows(),
        )
    written, skipped = (total, 0) if output_data is not None else (0, total)
    print(f"gap tiles (no archive entry at all): "
          f"filled {written}, skipped {skipped}", file=sys.stderr)
    return written, skipped


def close_connections(connections):
    for connection in connections:
        connection.commit()
        connection.close()
