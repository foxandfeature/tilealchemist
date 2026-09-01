#!/usr/bin/env python3
"""Runs once, before any shard worker: walks a source PMTiles archive's
directory tree and partitions the resulting entries (plus computed gaps,
see `compute_gaps()`) into `--worker-count` contiguous manifests, one per
worker. See README.md ("Fetching") for why it's structured this way, and
tilealchemist/sources/ for how the archive URL itself gets resolved.

    tilealchemist-prepare-shards --worker-count 128 --min-zoom 0 --max-zoom 14 \
        --out-dir manifests/
"""
import argparse
import json
import os
import sys

from pmtiles.tile import deserialize_directory, deserialize_header, zxy_to_tileid

from tilealchemist.manifest import Entry, write_manifest
from tilealchemist.ranged_fetch import (
    INDEX_READ_TIMEOUT,
    DownloadProgress,
    fetch_range,
    make_session,
)
from tilealchemist.sources import SOURCES, resolve_source
from tilealchemist.throttle import UpdateLineThrottle

# zxy_to_tileid() raises OverflowError above z=31 (tile_id stops fitting a
# 64-bit int), and we always ask it for max_zoom + 1, so max_zoom itself
# must stay at 30 or below.
MAX_SUPPORTED_ZOOM = 30

# The PMTiles header is a fixed 127 bytes at the very start of the archive.
PMTILES_HEADER_LENGTH = 127

# How often (seconds) the directory-walk update line is allowed to print.
WALK_LOG_INTERVAL = 1.0

# How often (seconds) the index-download update line is allowed to print.
# The index fetch is one blocking Range request (see collect_entries()), so
# unlike WALK_LOG_INTERVAL this is checked from inside the chunked read loop
# itself. Shorter than WALK_LOG_INTERVAL: it's a single network transfer, so
# a stall is worth surfacing sooner than a slow decode (same reasoning as
# build_shard.py's DOWNLOAD_REPORT_INTERVAL).
DOWNLOAD_LOG_INTERVAL = 1.0

# Names this module's ranged fetches in retry warnings (see ranged_fetch.py).
RETRY_LABEL = "prepare-shards"


def zoom_level_type(value):
    """argparse type shared by --min-zoom and --max-zoom: both accept
    exactly the same range, so they get exactly one validator."""
    zoom = int(value)
    if not (0 <= zoom <= MAX_SUPPORTED_ZOOM):
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_SUPPORTED_ZOOM}")
    return zoom


def tile_id_bounds(min_zoom, max_zoom):
    """The half-open tile-ID range [start, limit) covering min_zoom..max_zoom
    inclusive. Derived in one place because both the walk (which prunes
    against these bounds) and compute_gaps() (which fills the untouched
    stretches between entries) have to agree on them exactly."""
    start = zxy_to_tileid(min_zoom, 0, 0) if min_zoom > 0 else 0
    limit = zxy_to_tileid(max_zoom + 1, 0, 0)
    return start, limit


def walk_directory_tree(root_directory, leaf_blob, tile_id_start, tile_id_limit):
    """Purely local now that every node's bytes are already in memory
    (root_directory decoded up front, the rest sliced out of leaf_blob): no
    network fan-out here, just decoding and tile-ID pruning. Prunes at both
    ends of tile-ID space, symmetric to how the upper (max_zoom) bound
    already worked before min_zoom existed: sibling entries in a directory
    are sorted and non-overlapping, so an entry's own tile_id is the minimum
    tile_id anywhere in its subtree, and the next sibling's tile_id (or
    tile_id_limit, past the last sibling) is an upper bound on it. That's
    enough to decide "entirely below tile_id_start" or "entirely at/above
    tile_id_limit" without ever decoding the subtree, so min_zoom shrinks the
    walk itself instead of just filtering its result.

    The walk has two phases with very different rhythms, and the progress
    check has to catch both: unpacking (deserialize_directory(), triggered
    by run_length=0 pointer entries) and scanning (frontier.pop(), where
    real entries actually get appended). On a large global archive the root
    directory is itself typically nothing but pointers - no real tile
    entries live that shallow - so root's own for-loop (the very first pop)
    can by itself decode every one of the thousands of leaf directories it
    points to, all before the while loop ever gets back around to popping
    any of them: entry count stays at 0 for that whole stretch no matter
    what, since popping is what scans for entries. Checking due() only
    inside the decode branch would leave that unpacking stretch dark;
    checking it only per-pop would symmetrically leave the later scanning
    stretch dark, since popping a pure-leaf directory triggers no further
    decodes at all. Hence both checks below, sharing one throttle:
    whichever phase is currently running is the one that keeps tripping it."""
    entries = []
    directories_decoded = 1  # root_directory itself, already decoded by the caller
    directories_popped = 0
    decoded_bytes = 0
    total_bytes = len(leaf_blob)
    throttle = UpdateLineThrottle(WALK_LOG_INTERVAL)
    frontier = [root_directory]

    def log_progress():
        if not throttle.due():
            return
        # Decoding (not scanning) is the slow part of the walk - see the
        # root-fan-out stretch described above, where directories_popped and
        # the entry count both sit still while thousands of leaf directories
        # get decoded. So the percentage tracks decoded_bytes against
        # leaf_blob's total size rather than entries or directories_popped.
        # It's still only an upper-bound estimate: tile_id pruning means the
        # walk can (and on a zoom-restricted run, will) finish without ever
        # decoding every byte of leaf_blob, so it can stop short of 100%.
        percent = f" (~{100 * decoded_bytes / total_bytes:.1f}%)" if total_bytes else ""
        print(f"decoded {directories_decoded} directories, {directories_popped} processed, "
              f"{len(entries)} entries so far{percent}", file=sys.stderr)

    while frontier:
        directory = frontier.pop()
        directories_popped += 1
        log_progress()
        for index, entry in enumerate(directory):
            if entry.tile_id >= tile_id_limit:
                break
            if entry.run_length == 0:
                next_tile_id = (directory[index + 1].tile_id if index + 1 < len(directory)
                                 else tile_id_limit)
                if next_tile_id > tile_id_start:
                    node_bytes = leaf_blob[entry.offset:entry.offset + entry.length]
                    frontier.append(deserialize_directory(node_bytes))
                    directories_decoded += 1
                    decoded_bytes += len(node_bytes)
                    log_progress()
            elif entry.tile_id + entry.run_length > tile_id_start:
                entries.append(entry)

    return entries


def collect_entries(session, url, min_zoom, max_zoom):
    """All directory entries covering min_zoom..max_zoom, in ascending
    *offset* order (see README.md "Fetching" for why offset order).

    Only 2 requests total, however deep the directory tree goes: the header,
    then one Range request spanning root directory + metadata + every leaf
    directory. That single span is safe because writers lay the file out
    Header/Root/Metadata/LeafDirs/TileData back-to-back with no gaps, and
    leaf_directory_offset/leaf_directory_length are already known from the
    127-byte header alone. So the whole index comes down in one transfer
    instead of one tiny latency-bound request per node discovered while
    walking (thousands of them on a full planet archive), and the tree is
    then walked and pruned entirely from that in-memory buffer."""
    header = deserialize_header(fetch_range(
        session, url, 0, PMTILES_HEADER_LENGTH,
        retry_label=RETRY_LABEL, timeout=INDEX_READ_TIMEOUT))

    index_start = header["root_offset"]
    index_end = header["leaf_directory_offset"] + header["leaf_directory_length"]
    index_length = index_end - index_start
    print(f"starting download ({index_length} bytes, {header['tile_entries_count']} entries)",
          file=sys.stderr)

    progress = DownloadProgress(index_length, DOWNLOAD_LOG_INTERVAL, "directory index")
    index_blob = fetch_range(
        session, url, index_start, index_length,
        retry_label=RETRY_LABEL, timeout=INDEX_READ_TIMEOUT,
        on_chunk=lambda chunk_length, _downloaded: progress.add(chunk_length))

    root_directory = deserialize_directory(index_blob[:header["root_length"]])
    leaf_blob = index_blob[header["leaf_directory_offset"] - index_start:]

    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    print(f"starting decode ({header['tile_entries_count']} entries expected)", file=sys.stderr)
    entries = walk_directory_tree(root_directory, leaf_blob, tile_id_start, tile_id_limit)
    entries.sort(key=lambda entry: entry.offset)
    return header, entries


# Gaps (see compute_gaps()) are chunked to at most this many tiles per
# manifest record so a single huge unbroken gap (e.g. a whole ice sheet's
# interior) doesn't land entirely on one worker.
GAP_CHUNK_SIZE = 200_000


def compute_gaps(entries, tile_id_start, tile_id_limit):
    """Gaps in [tile_id_start, tile_id_limit) not covered by any entry (see
    README.md "Fetching"). Directory entries never overlap in tile_id space,
    so once sorted by tile_id, each entry's end is always >= the previous
    one's, so `expected` can just be overwritten every iteration below
    instead of tracked as a running max."""
    entries_by_tile_id = sorted(entries, key=lambda entry: entry.tile_id)
    gaps = []
    expected = tile_id_start
    for entry in entries_by_tile_id:
        if entry.tile_id > expected:
            gaps.extend(_chunk_gap(expected, entry.tile_id))
        expected = entry.tile_id + entry.run_length
    if expected < tile_id_limit:
        gaps.extend(_chunk_gap(expected, tile_id_limit))
    return gaps


def _chunk_gap(start, end):
    chunks = []
    while start < end:
        chunk_size = min(GAP_CHUNK_SIZE, end - start)
        chunks.append(Entry(tile_id=start, offset=0, length=0, run_length=chunk_size))
        start += chunk_size
    return chunks


def partition_by_index(entries, worker_count):
    """Splits gap records into `worker_count` even chunks by plain record
    count. Can't reuse partition_contiguous()'s offset-adjacency rule here:
    every gap record shares offset=0 (a sentinel, not a real byte position),
    so that rule would lump them all into a single block instead of
    spreading them out."""
    entry_count = len(entries)
    blocks = []
    for worker_index in range(worker_count):
        start_index = entry_count * worker_index // worker_count
        end_index = entry_count * (worker_index + 1) // worker_count
        blocks.append(entries[start_index:end_index])
    return blocks


def partition_contiguous(entries, worker_count):
    """Splits `entries` (sorted by offset) into `worker_count` contiguous
    blocks of about entry_count/worker_count *entries* each - not
    total_length/worker_count *bytes* and not total_run_length/worker_count
    *output tiles* either.

    Entry count is the right unit because decode is paid once per entry no
    matter its byte length or its run_length, so entry count is what
    actually tracks how many of those decode calls a worker gets stuck
    with. Two earlier attempts got this wrong in opposite directions.
    Balancing on bytes let one real run assign a worker 3.5M real entries
    against its peers' 500K-900K (near-identical download size, ~5x the
    decode work), which ran that worker out of memory. Balancing on
    run_length (total output tiles) made it worse: a handful of entries with
    a huge run_length "fills" a tile-sized target almost immediately while
    costing almost no decode work, so regions dense in those pushed every
    real, unique-content entry (run_length 1, one decode each - and just as
    many bytes to fetch) onto whatever workers were left, producing a
    4.3M-entry/16GB worker next to a 76K-entry/7MB one.

    build_shard.py's `_chunk_entries()` splits a worker's own share across
    processes on the same reasoning; that's the one other place this
    tradeoff applies.

    It's only "about" because a whole run of same-offset entries always goes
    to one worker, even past the target size (see README.md "Fetching")."""
    entry_count = len(entries)
    blocks = [[] for _ in range(worker_count)]
    if entry_count == 0:
        return blocks
    worker_index = 0
    index = 0
    assigned_count = 0
    while index < entry_count:
        run_end = index + 1
        while run_end < entry_count and entries[run_end].offset == entries[index].offset:
            run_end += 1
        run = entries[index:run_end]
        blocks[worker_index].extend(run)
        assigned_count += len(run)
        index = run_end
        target_count = entry_count * (worker_index + 1) // worker_count
        if assigned_count >= target_count and worker_index < worker_count - 1:
            worker_index += 1
    return blocks


def partition_into_worker_blocks(entries, gaps, worker_count):
    """Each worker's full share: its contiguous slice of real entries plus
    its even slice of gap records. Kept separate because the two obey
    different balancing rules (see partition_contiguous/partition_by_index)
    but are written to one manifest per worker."""
    real_blocks = partition_contiguous(entries, worker_count)
    gap_blocks = partition_by_index(gaps, worker_count)
    return [real_blocks[worker_index] + gap_blocks[worker_index]
            for worker_index in range(worker_count)]


def write_worker_manifests(out_dir, blocks):
    for worker_index, block in enumerate(blocks):
        write_manifest(os.path.join(out_dir, f"worker-{worker_index:03d}.bin"), block)


def write_source_metadata(out_dir, resolved_source, min_zoom, max_zoom, tile_data_offset):
    """The one file every build_shard.py worker reads besides its own
    manifest: which archive to fetch from, and the zoom bounds and tile-data
    base offset its manifest's offsets are relative to."""
    with open(os.path.join(out_dir, "source.json"), "w") as source_file:
        json.dump({
            "url": resolved_source.url,
            "build": resolved_source.build,
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "tile_data_offset": tile_data_offset,
        }, source_file)


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-count", type=int, default=128)
    parser.add_argument("--min-zoom", type=zoom_level_type, default=0)
    parser.add_argument("--max-zoom", type=zoom_level_type, default=14)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source", choices=sorted(SOURCES), default="openfreemap",
                         help="where to resolve the PMTiles archive from (default openfreemap)")
    parser.add_argument("--source-url", default=None,
                         help="the PMTiles URL to use, required when --source static-url")
    args = parser.parse_args()

    if args.min_zoom > args.max_zoom:
        parser.error(f"--min-zoom ({args.min_zoom}) must not exceed --max-zoom ({args.max_zoom})")
    return args


def main():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    resolved_source = resolve_source(args.source, args.source_url).resolve()
    print(f"source={resolved_source.url} (build {resolved_source.build})", file=sys.stderr)

    session = make_session()
    header, entries = collect_entries(session, resolved_source.url, args.min_zoom, args.max_zoom)
    print(f"directory walk found {len(entries)} distinct tile entries "
          f"(min_zoom={args.min_zoom}, max_zoom={args.max_zoom})", file=sys.stderr)

    tile_id_start, tile_id_limit = tile_id_bounds(args.min_zoom, args.max_zoom)
    gaps = compute_gaps(entries, tile_id_start, tile_id_limit)
    gap_tile_count = sum(gap.run_length for gap in gaps)
    print(f"{len(gaps)} gap ranges covering {gap_tile_count} tiles with no archive "
          f"entry at all", file=sys.stderr)

    blocks = partition_into_worker_blocks(entries, gaps, args.worker_count)
    write_worker_manifests(args.out_dir, blocks)
    write_source_metadata(args.out_dir, resolved_source, args.min_zoom, args.max_zoom,
                          header["tile_data_offset"])

    non_empty_count = sum(1 for block in blocks if block)
    print(f"wrote {len(blocks)} manifests to {args.out_dir} "
          f"({non_empty_count} non-empty)", file=sys.stderr)


if __name__ == "__main__":
    main()
