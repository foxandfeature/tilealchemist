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
import time

import requests
from pmtiles.tile import deserialize_directory, deserialize_header, zxy_to_tileid

from tilealchemist.backoff import backoff_delay
from tilealchemist.manifest import Entry, write_manifest
from tilealchemist.sources import SOURCES, resolve_source
from tilealchemist.throttle import UpdateLineThrottle

# zxy_to_tileid() raises OverflowError above z=31 (tile_id stops fitting a
# 64-bit int), and we always ask it for max_zoom + 1, so max_zoom itself
# must stay at 30 or below.
MAX_SUPPORTED_ZOOM = 30

# root_offset..root_offset+root_length (root directory), metadata, and
# leaf_directory_offset..+leaf_directory_length (every non-root directory
# node, however deep the tree goes) are packed back-to-back with no gaps -
# writers lay the file out Header/Root/Metadata/LeafDirs/TileData in that
# order - and leaf_directory_offset/leaf_directory_length are already known
# from the 127-byte header alone. So the whole index (root + every leaf
# directory) can be pulled down as a single Range request instead of one
# tiny latency-bound request per node discovered while walking (thousands of
# them on a full planet archive), and the tree then walked/pruned entirely
# from that in-memory buffer with zero further network requests.
WALK_LOG_INTERVAL = 5.0

# Retries handle two transient cold-cache-stampede responses: a 200 instead
# of 206 (server ignored Range, the same check as build_shard.py's
# fetch_batch_streaming_with_retries(), since reading that in full would be tens of GB),
# and 429/5xx rate-limiting. Both worth backing off and retrying, not
# failing immediately.
MAX_RANGE_ATTEMPTS = 6
RANGE_RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def make_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount("https://", adapter)
    return session


def _wait_before_retry(attempt, response, range_header, reason=""):
    """Prints the retry directly as a `::warning::` workflow command (rather
    than a plain line now and a replayed `::warning::` later) so a run that
    hit the OpenFreeMap CDN's cold-cache stampede shows up as an actual
    warning status in the Actions UI as it happens, with no duplicate line.
    Safe to call unprotected from the many concurrent walk threads: CPython
    serializes the underlying print()."""
    delay = backoff_delay(attempt, response, RANGE_RETRY_BASE_DELAY)
    print(f"::warning title=prepare-shards retry::got HTTP {response.status_code}{reason} "
          f"for range {range_header} (attempt {attempt}/{MAX_RANGE_ATTEMPTS}), "
          f"retrying in {delay:.0f}s", file=sys.stderr)
    response.close()
    time.sleep(delay)


def _fetch_range_attempt(session, url, range_header, attempt):
    """Returns the response body on success, or None if `_wait_before_retry`
    already handled the backoff and the caller should try again."""
    with session.get(url, headers={"Range": range_header}, timeout=60, stream=True) as response:
        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt == MAX_RANGE_ATTEMPTS:
                response.raise_for_status()
            _wait_before_retry(attempt, response, range_header)
            return None

        response.raise_for_status()
        if response.status_code != 206:
            if attempt == MAX_RANGE_ATTEMPTS:
                raise RuntimeError(
                    f"expected HTTP 206 Partial Content for ranged request ({range_header}) "
                    f"after {MAX_RANGE_ATTEMPTS} attempts, got {response.status_code}: server "
                    f"ignored the Range header and would send the entire archive instead of "
                    f"just this range"
                )
            _wait_before_retry(attempt, response, range_header, reason=" instead of 206")
            return None

        return response.content


def fetch_range(session, url, offset, length):
    range_header = f"bytes={offset}-{offset + length - 1}"
    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        result = _fetch_range_attempt(session, url, range_header, attempt)
        if result is not None:
            return result


def walk_directory_tree(root_directory, leaf_blob, tile_id_start, tile_id_limit):
    """Purely local now that every node's bytes are already in memory
    (root_directory decoded up front, the rest sliced out of leaf_blob): no
    more network fan-out here, just decoding and the same tile-ID pruning
    as before. Prunes at both ends of tile-ID space, symmetric to how the
    upper (max_zoom) bound already worked before min_zoom existed: sibling
    entries in a directory are sorted and non-overlapping, so an entry's own
    tile_id is the minimum tile_id anywhere in its subtree, and the next
    sibling's tile_id (or tile_id_limit, past the last sibling) is an upper
    bound on it. That's enough to decide "entirely below tile_id_start" or
    "entirely at/above tile_id_limit" without ever decoding the subtree, so
    min_zoom shrinks the walk itself instead of just filtering its result."""
    entries = []
    dirs_walked = 0
    throttle = UpdateLineThrottle(WALK_LOG_INTERVAL)
    frontier = [root_directory]

    while frontier:
        directory = frontier.pop()
        dirs_walked += 1
        for index, entry in enumerate(directory):
            if entry.tile_id >= tile_id_limit:
                break
            if entry.run_length == 0:
                next_tile_id = (directory[index + 1].tile_id if index + 1 < len(directory)
                                 else tile_id_limit)
                if next_tile_id > tile_id_start:
                    node_bytes = leaf_blob[entry.offset:entry.offset + entry.length]
                    frontier.append(deserialize_directory(node_bytes))
            elif entry.tile_id + entry.run_length > tile_id_start:
                entries.append(entry)

        if throttle.due():
            print(f"walked {dirs_walked} directories, {len(entries)} entries so far",
                  file=sys.stderr)

    return entries


def collect_entries(session, url, min_zoom, max_zoom):
    """All directory entries covering min_zoom..max_zoom, in ascending
    *offset* order (see README.md "Fetching" for why offset order). Only 2
    requests total: the header, then one Range request spanning root
    directory + metadata + every leaf directory (see the comment above
    WALK_LOG_INTERVAL for why that's safe to fetch as one contiguous span)."""
    header = deserialize_header(fetch_range(session, url, 0, 127))

    index_start = header["root_offset"]
    index_end = header["leaf_directory_offset"] + header["leaf_directory_length"]
    index_blob = fetch_range(session, url, index_start, index_end - index_start)

    root_directory = deserialize_directory(index_blob[:header["root_length"]])
    leaf_blob = index_blob[header["leaf_directory_offset"] - index_start:]

    tile_id_start = zxy_to_tileid(min_zoom, 0, 0) if min_zoom > 0 else 0
    tile_id_limit = zxy_to_tileid(max_zoom + 1, 0, 0)
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
    by_tileid = sorted(entries, key=lambda e: e.tile_id)
    gaps = []
    expected = tile_id_start
    for entry in by_tileid:
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
    blocks = [[] for _ in range(worker_count)]
    for worker in range(worker_count):
        start_index = entry_count * worker // worker_count
        end_index = entry_count * (worker + 1) // worker_count
        blocks[worker] = entries[start_index:end_index]
    return blocks


def partition_contiguous(entries, worker_count):
    """Splits `entries` (sorted by offset) into `worker_count` contiguous
    blocks of about entry_count/W *entries* each - not total_length/W
    *bytes* and not total_run_length/W *output tiles* either: decode is
    paid once per entry no matter its byte length or its run_length, so
    entry count is what actually tracks how many of those decode calls a
    worker gets stuck with. Two earlier attempts got this wrong in
    opposite directions. Balancing on bytes let one real run assign a
    worker 3.5M real entries against its peers' 500K-900K (near-identical
    download size, ~5x the decode work), which ran that worker out of
    memory. Balancing on run_length (total output tiles) made it worse:
    a handful of entries with a huge run_length "fills" a tile-sized
    target almost immediately while costing almost no decode work, so
    regions dense in those pushed every real, unique-content entry
    (run_length 1, one decode each - and just as many bytes to fetch) onto
    whatever workers were left, producing a 4.3M-entry/16GB worker next to
    a 76K-entry/7MB one. It's only "about" because a whole run of
    same-offset entries always goes to one worker, even past the target
    size (see README.md "Fetching")."""
    entry_count = len(entries)
    blocks = [[] for _ in range(worker_count)]
    if entry_count == 0:
        return blocks
    worker = 0
    index = 0
    cumulative_count = 0
    while index < entry_count:
        run_end = index + 1
        while run_end < entry_count and entries[run_end].offset == entries[index].offset:
            run_end += 1
        run = entries[index:run_end]
        blocks[worker].extend(run)
        cumulative_count += len(run)
        index = run_end
        target_count = entry_count * (worker + 1) // worker_count
        if cumulative_count >= target_count and worker < worker_count - 1:
            worker += 1
    return blocks


def max_zoom_type(value):
    zoom = int(value)
    if not (0 <= zoom <= MAX_SUPPORTED_ZOOM):
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_SUPPORTED_ZOOM}")
    return zoom


def min_zoom_type(value):
    zoom = int(value)
    if not (0 <= zoom <= MAX_SUPPORTED_ZOOM):
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_SUPPORTED_ZOOM}")
    return zoom


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-count", type=int, default=128)
    parser.add_argument("--min-zoom", type=min_zoom_type, default=0)
    parser.add_argument("--max-zoom", type=max_zoom_type, default=14)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source", choices=sorted(SOURCES), default="openfreemap",
                         help="where to resolve the PMTiles archive from (default openfreemap)")
    parser.add_argument("--source-url", default=None,
                         help="the PMTiles URL to use, required when --source static-url")
    args = parser.parse_args()

    if args.min_zoom > args.max_zoom:
        parser.error(f"--min-zoom ({args.min_zoom}) must not exceed --max-zoom ({args.max_zoom})")

    os.makedirs(args.out_dir, exist_ok=True)

    # resolve source archive
    resolved = resolve_source(args.source, args.source_url).resolve()
    url, timestamp = resolved.url, resolved.build
    print(f"source={url} (build {timestamp})", file=sys.stderr)

    # one-time directory walk
    session = make_session()
    header, entries = collect_entries(session, url, args.min_zoom, args.max_zoom)
    print(f"directory walk found {len(entries)} distinct tile entries "
          f"(min_zoom={args.min_zoom}, max_zoom={args.max_zoom})", file=sys.stderr)

    # find genuinely-absent tiles (gaps)
    tile_id_start = zxy_to_tileid(args.min_zoom, 0, 0) if args.min_zoom > 0 else 0
    tile_id_limit = zxy_to_tileid(args.max_zoom + 1, 0, 0)
    gaps = compute_gaps(entries, tile_id_start, tile_id_limit)
    gap_tile_count = sum(gap.run_length for gap in gaps)
    print(f"{len(gaps)} gap ranges covering {gap_tile_count} tiles with no archive "
          f"entry at all", file=sys.stderr)

    # partition into per-worker manifests
    real_blocks = partition_contiguous(entries, args.worker_count)
    gap_blocks = partition_by_index(gaps, args.worker_count)
    blocks = [real_blocks[worker_index] + gap_blocks[worker_index]
              for worker_index in range(args.worker_count)]
    for worker_index, block in enumerate(blocks):
        write_manifest(os.path.join(args.out_dir, f"worker-{worker_index:03d}.bin"), block)

    # write source metadata for build_shard.py
    with open(os.path.join(args.out_dir, "source.json"), "w") as file:
        json.dump({
            "url": url,
            "build": timestamp,
            "min_zoom": args.min_zoom,
            "max_zoom": args.max_zoom,
            "tile_data_offset": header["tile_data_offset"],
        }, file)

    non_empty = sum(1 for block in blocks if block)
    print(f"wrote {len(blocks)} manifests to {args.out_dir} "
          f"({non_empty} non-empty)", file=sys.stderr)


if __name__ == "__main__":
    main()
