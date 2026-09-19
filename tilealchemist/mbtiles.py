"""One shard database per profile, and the writes that fill it."""
import json
import sqlite3
import sys

from pmtiles.tile import tileid_to_zxy

INSERT_TILE = ("INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
               "VALUES (?, ?, ?, ?)")


class ProfileTileCounts:

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
        # mbtiles numbers rows TMS-style, PMTiles tile IDs decode to XYZ.
        tms_row = (2 ** zoom - 1) - tile_row
        connection.execute(INSERT_TILE, (zoom, tile_column, tms_row, output_data))
        written += 1
    return written, skipped


def write_gap_tiles(gap_entries, connection, output_data):
    """Writes the profile's single gap answer at every gap tile, or nothing when None."""
    total = sum(entry.run_length for entry in gap_entries)

    # A generator: an ocean-sized gap must not become one list before sqlite3 sees it.
    def tms_rows():
        for entry in gap_entries:
            for run_offset in range(entry.run_length):
                zoom, tile_column, tile_row = tileid_to_zxy(entry.tile_id + run_offset)
                yield (zoom, tile_column, (2 ** zoom - 1) - tile_row, output_data)

    if output_data is None:
        written, skipped = 0, total
    else:
        connection.executemany(INSERT_TILE, tms_rows())
        written, skipped = total, 0
    print(f"gap tiles (no archive entry at all): "
          f"filled {written}, skipped {skipped}", file=sys.stderr)
    return written, skipped


def close_connections(connections):
    for connection in connections:
        connection.commit()
        connection.close()
