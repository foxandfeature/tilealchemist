"""The source archive's directory index: a URL in, every directory entry
covering min_zoom..max_zoom out, in ascending offset order.

Nothing here decides what to do with those entries; `prepare_shards.py`
drives the run and hands what comes out of here to `partition.py`. See
docs/ARCHITECTURE.md's "Fetching" section for why only the part of the index
the zoom range needs comes down, and why both zoom bounds prune the walk
itself rather than just its result.

    collect_entries()          two requests, whatever the archive's size
      tile_id_bounds()         the [start, limit) the walk prunes against
      leaf_window_for()        which leaf directories that range needs
      walk_directory_tree()    root + leaf directories, decoded in memory
        LeafWindow             the leaf bytes that came down
        WalkProgress           throttled `update: ...` lines
"""
import sys

from pmtiles.tile import deserialize_directory, deserialize_header, zxy_to_tileid

from tilealchemist.ranged_fetch import DownloadProgress, fetch_range
from tilealchemist.throttle import UpdateLineThrottle

# The PMTiles header is a fixed 127 bytes at the very start of the archive.
PMTILES_HEADER_LENGTH = 127

# How much of the archive's start collect_entries() asks for: the 127-byte
# header plus the root directory behind it. PMTiles v3 (spec section 4)
# requires the root to be "contained in the first 16,384 bytes" so that a
# client can fetch it in one go without knowing its size, which is exactly
# what this does -- so no conforming archive needs a second request for it.
HEADER_AND_ROOT_PREFIX_LENGTH = 16 * 1024

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
        # Only decoded bytes have a total to divide by (the leaf window's
        # length), so they carry the percentage; pops and appended entries
        # have no denominator. That total is an upper bound: tile_id pruning
        # lets the walk finish without decoding all of the window, so the
        # percentage can stop short of 100%.
        percent = (f" (~{100 * self.decoded_bytes / self.total_bytes:.1f}%)"
                    if self.total_bytes else "")
        print(f"update: decoded {self.directories_decoded} directories, "
              f"{self.directories_popped} processed, "
              f"{len(self.entries)} entries so far{percent}", file=sys.stderr)


class LeafWindow:
    """The stretch of the archive's leaf-directory section that came down, and
    how far into that section it begins.

    A directory entry's offset is relative to the section, not to this
    stretch, so every read goes through `node_bytes()`. Its bounds check is
    the one thing standing between an archive laid out unlike the two the
    window's pruning assumes and a silently short (or negative-index) slice
    that would decode into plausible-looking garbage, so it raises instead.
    """

    def __init__(self, blob, start):
        self.blob = blob
        self.start = start

    def node_bytes(self, entry):
        offset = entry.offset - self.start
        if offset < 0 or offset + entry.length > len(self.blob):
            raise RuntimeError(
                f"directory at leaf-section offset {entry.offset} (+{entry.length} bytes) "
                f"lies outside the {len(self.blob)} bytes fetched from {self.start}. "
                f"`leaf_window_for()` spans what the root points at, in the file order "
                f"PMTiles v3 section 4 asks for -- leaf order SHOULD ascend by TileID, and "
                f"more than one level of leaf directories is discouraged. This archive "
                f"breaks one of the two; reading it needs the whole leaf section.")
        return self.blob[offset:offset + entry.length]


def leaf_window_for(root_directory, tile_id_start, tile_id_limit):
    """The `(start, length)` byte range of the leaf-directory section spanned
    by the root's own pointers into [tile_id_start, tile_id_limit), or (0, 0)
    when the root points at no directory in that range at all.

    Prunes by exactly the rule `walk_directory_tree()` descends by (kept in
    step by hand: the walk pays it per entry over a whole planet's worth of
    directories, this pays it over the root's few thousand). Leaf directories
    sit in the file in root-pointer order, so the ones the walk will reach run
    from the first match to the last, and those two ends are what this returns.
    An archive ordering them otherwise trips the walk's bounds check, which is
    deliberate: an error beats carrying a wider window for a layout PMTiles
    asks writers not to produce.
    """
    start = end = None
    for index, entry in enumerate(root_directory):
        if entry.tile_id >= tile_id_limit:
            break
        if entry.run_length != 0:
            continue
        next_tile_id = (root_directory[index + 1].tile_id
                        if index + 1 < len(root_directory) else tile_id_limit)
        if next_tile_id <= tile_id_start:
            continue
        if start is None:
            start = entry.offset
        end = entry.offset + entry.length
    return (0, 0) if start is None else (start, end - start)


def tile_id_bounds(min_zoom, max_zoom):
    """The half-open tile-ID range [start, limit) covering min_zoom..max_zoom
    inclusive. Derived in one place because both the walk (which prunes
    against these bounds) and partition.py's compute_gaps() (which fills the
    untouched stretches between entries) have to agree on them exactly."""
    return zxy_to_tileid(min_zoom, 0, 0), zxy_to_tileid(max_zoom + 1, 0, 0)


def walk_directory_tree(root_directory, leaf_window, tile_id_start, tile_id_limit):
    """Every entry in [tile_id_start, tile_id_limit), walked from memory: the
    root arrives already decoded and every other node is a slice of
    `leaf_window`, so nothing here touches the network. A walk that reaches a
    directory the window doesn't hold raises, rather than decoding whatever
    bytes happen to sit at that offset.

    The bounds prune the walk itself, not its result. Siblings are sorted and
    non-overlapping, so an entry's tile_id is the lowest in its subtree and
    the next sibling's tile_id (or tile_id_limit, past the last one) bounds
    it from above, so a subtree outside the range is skipped undecoded."""
    entries = []
    progress = WalkProgress(len(leaf_window.blob), entries)
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
                    node_bytes = leaf_window.node_bytes(entry)
                    frontier.append(deserialize_directory(node_bytes))
                    progress.decoded(node_bytes)
            elif entry.tile_id + entry.run_length > tile_id_start:
                entries.append(entry)

    return entries


def collect_entries(session, url, min_zoom, max_zoom):
    """Every directory entry covering min_zoom..max_zoom, in ascending
    *offset* order (see docs/ARCHITECTURE.md "Fetching" for why offset order),
    plus the header they came from (prepare_shards.py needs its
    tile_data_offset).

    Two requests, always: a 16 KB prefix holding header and root, then the
    stretch of leaf directories the zoom range needs. See
    docs/ARCHITECTURE.md "Fetching" for why both the batching and the pruning.

    No file layout is assumed; the header says where root and leaf section
    each begin, so an archive that puts its leaves after the tile data
    (Protomaps' builds do) reads the same as one that doesn't.
    """
    header, root_directory = _fetch_header_and_root(session, url)
    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    leaf_window = _fetch_leaf_window(
        session, url, header, *leaf_window_for(root_directory, tile_id_start, tile_id_limit))

    print(f"starting decode ({header['tile_entries_count']} entries expected)", file=sys.stderr)
    entries = walk_directory_tree(root_directory, leaf_window, tile_id_start, tile_id_limit)
    entries.sort(key=lambda entry: entry.offset)
    return header, entries


def _fetch_header_and_root(session, url):
    """The archive's header and its root directory decoded, in one request:
    the prefix is the size PMTiles guarantees both fit inside, and the header
    is what says how much of it the root actually occupies."""
    prefix = fetch_range(session, url, 0, HEADER_AND_ROOT_PREFIX_LENGTH,
                         retry_label=RETRY_LABEL)
    header = deserialize_header(prefix[:PMTILES_HEADER_LENGTH])

    root_start, root_length = header["root_offset"], header["root_length"]
    if root_start + root_length > len(prefix):
        raise RuntimeError(
            f"root directory runs to byte {root_start + root_length}, past the "
            f"{len(prefix)} fetched: PMTiles v3 section 4 requires header plus root "
            f"inside the first {HEADER_AND_ROOT_PREFIX_LENGTH} bytes")
    return header, deserialize_directory(prefix[root_start:root_start + root_length])


def _fetch_leaf_window(session, url, header, window_start, window_length):
    """`window_length` bytes of the archive's leaf-directory section, starting
    `window_start` bytes into it. A zero-length window means the root answered
    the whole zoom range by itself, and nothing is fetched at all."""
    if window_length == 0:
        return LeafWindow(b"", 0)

    print(f"starting download ({window_length} bytes of leaf directories, "
          f"{header['tile_entries_count']} entries in the archive)", file=sys.stderr)
    blob = fetch_range(
        session, url, header["leaf_directory_offset"] + window_start, window_length,
        retry_label=RETRY_LABEL,
        on_chunk=DownloadProgress(window_length, LOG_INTERVAL, "directory index").update)
    return LeafWindow(blob, window_start)
