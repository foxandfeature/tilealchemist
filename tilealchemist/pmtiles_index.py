"""The source archive's directory index: a URL in, every directory entry
covering min_zoom..max_zoom out, in ascending offset order.

Nothing here decides what to do with those entries; `prepare_shards.py`
drives the run and hands what comes out of here to `partition.py`. See
docs/ARCHITECTURE.md's "Fetching" section for why the whole index comes down
in a single Range request and why both zoom bounds prune the walk itself
rather than just its result.

    collect_entries()          two requests, however deep the tree
      tile_id_bounds()         the [start, limit) the walk prunes against
      walk_directory_tree()    root + leaf directories, decoded in memory
        WalkProgress           throttled `update: ...` lines
"""
import sys

from pmtiles.tile import deserialize_directory, deserialize_header, zxy_to_tileid

from tilealchemist.ranged_fetch import DownloadProgress, fetch_range
from tilealchemist.throttle import UpdateLineThrottle

# zxy_to_tileid() raises OverflowError above z=31 (tile_id stops fitting a
# 64-bit int), and tile_id_bounds() always asks it for max_zoom + 1, so
# max_zoom itself must stay at 30 or below.
MAX_SUPPORTED_ZOOM = 30

# The PMTiles header is a fixed 127 bytes at the very start of the archive.
PMTILES_HEADER_LENGTH = 127

# How often (seconds) this module's update lines are allowed to print, for
# both the index download and the directory walk.
LOG_INTERVAL = 1.0

# Names this module's ranged fetches in retry warnings (see ranged_fetch.py).
RETRY_LABEL = "prepare-shards"


class WalkProgress:
    """Throttled `update: ...` lines for one directory walk, counting off the
    `entries` list the walk is filling.

    Both of the walk's phases report, because either can be the one taking
    the time: decoding (thousands of leaf directories, which on a global
    archive's pointer-only root all get unpacked before a single entry is
    appended) and scanning (the pops that append entries, which for a
    pure-leaf directory decode nothing at all). One shared throttle, so
    whichever phase is currently running is the one tripping it.
    """

    def __init__(self, total_bytes, entries):
        self.total_bytes = total_bytes
        self.entries = entries
        self.directories_decoded = 1  # the root, already decoded by the caller
        self.directories_popped = 0
        self.decoded_bytes = 0
        self.throttle = UpdateLineThrottle(LOG_INTERVAL)

    def decoded(self, node_bytes):
        self.directories_decoded += 1
        self.decoded_bytes += len(node_bytes)
        self.report()

    def popped(self):
        self.directories_popped += 1
        self.report()

    def report(self):
        if not self.throttle.due():
            return
        # Only decoded bytes have a total to divide by (leaf_blob's length),
        # so they carry the percentage; pops and appended entries have no
        # denominator. That total is an upper bound: tile_id pruning lets the
        # walk finish without decoding all of leaf_blob, so the percentage
        # can stop short of 100%.
        percent = (f" (~{100 * self.decoded_bytes / self.total_bytes:.1f}%)"
                    if self.total_bytes else "")
        print(f"update: decoded {self.directories_decoded} directories, "
              f"{self.directories_popped} processed, "
              f"{len(self.entries)} entries so far{percent}", file=sys.stderr)


def tile_id_bounds(min_zoom, max_zoom):
    """The half-open tile-ID range [start, limit) covering min_zoom..max_zoom
    inclusive. Derived in one place because both the walk (which prunes
    against these bounds) and partition.py's compute_gaps() (which fills the
    untouched stretches between entries) have to agree on them exactly."""
    return zxy_to_tileid(min_zoom, 0, 0), zxy_to_tileid(max_zoom + 1, 0, 0)


def walk_directory_tree(root_directory, leaf_blob, tile_id_start, tile_id_limit):
    """Every entry in [tile_id_start, tile_id_limit), walked from memory: the
    root arrives already decoded and every other node is a slice of
    leaf_blob, so nothing here touches the network.

    The bounds prune the walk itself, not its result. Siblings are sorted and
    non-overlapping, so an entry's tile_id is the lowest in its subtree and
    the next sibling's tile_id (or tile_id_limit, past the last one) bounds
    it from above, so a subtree outside the range is skipped undecoded."""
    entries = []
    progress = WalkProgress(len(leaf_blob), entries)
    frontier = [root_directory]

    while frontier:
        directory = frontier.pop()
        progress.popped()
        for index, entry in enumerate(directory):
            if entry.tile_id >= tile_id_limit:
                break
            if entry.run_length == 0:
                next_tile_id = (directory[index + 1].tile_id if index + 1 < len(directory)
                                 else tile_id_limit)
                if next_tile_id > tile_id_start:
                    node_bytes = leaf_blob[entry.offset:entry.offset + entry.length]
                    frontier.append(deserialize_directory(node_bytes))
                    progress.decoded(node_bytes)
            elif entry.tile_id + entry.run_length > tile_id_start:
                entries.append(entry)

    return entries


def collect_entries(session, url, min_zoom, max_zoom):
    """Every directory entry covering min_zoom..max_zoom, in ascending
    *offset* order (see README.md "Fetching" for why offset order), plus the
    header they came from (prepare_shards.py needs its tile_data_offset).

    Two requests total, however deep the directory tree goes: the header,
    then one Range request spanning root directory + metadata + every leaf
    directory. That single span is safe because writers lay the file out
    Header/Root/Metadata/LeafDirs/TileData back-to-back with no gaps, and the
    127-byte header alone gives its extent. So the whole index arrives in one
    transfer instead of one tiny latency-bound request per node discovered
    while walking, thousands of them on a full planet archive."""
    header = deserialize_header(fetch_range(
        session, url, 0, PMTILES_HEADER_LENGTH, retry_label=RETRY_LABEL))

    index_start = header["root_offset"]
    index_length = header["leaf_directory_offset"] + header["leaf_directory_length"] - index_start
    print(f"starting download ({index_length} bytes, {header['tile_entries_count']} entries)",
          file=sys.stderr)
    index_blob = fetch_range(
        session, url, index_start, index_length,
        retry_label=RETRY_LABEL,
        on_chunk=DownloadProgress(index_length, LOG_INTERVAL, "directory index").update)

    root_directory = deserialize_directory(index_blob[:header["root_length"]])
    leaf_blob = index_blob[header["leaf_directory_offset"] - index_start:]

    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    print(f"starting decode ({header['tile_entries_count']} entries expected)", file=sys.stderr)
    entries = walk_directory_tree(root_directory, leaf_blob, tile_id_start, tile_id_limit)
    entries.sort(key=lambda entry: entry.offset)
    return header, entries
