#!/usr/bin/env python3
"""Runs once, before any shard worker: walks a source PMTiles archive's
directory tree and partitions the resulting entries (plus computed gaps,
see `compute_gaps()`) into `--worker-count` contiguous manifests, one per
worker. See README.md ("Fetching") for why it's structured this way, and
tilealchemist/sources/ for how the archive URL itself gets resolved.

    tilealchemist-prepare-shards --worker-count 64 --min-zoom 0 --max-zoom 14 \
        --out-dir manifests/
"""
import argparse
import concurrent.futures
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

# Latency-bound tiny fetches, so concurrency helps. It's kept well below
# --worker-count (default 64) so this one-time walk doesn't add extra load
# on top of the much larger per-shard fetch fan-out that follows, on
# OpenFreeMap's single free community-run server.
WALK_WORKER_COUNT = 32
WALK_LOG_INTERVAL = 5.0

# Retries handle two transient cold-cache-stampede responses: a 200 instead
# of 206 (server ignored Range, the same check as build_shard.py's
# fetch_batch_streaming_with_retries(), since reading that in full would be tens of GB),
# and 429/5xx rate-limiting. Both worth backing off and retrying, not
# failing immediately.
MAX_RANGE_ATTEMPTS = 6
RANGE_RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def make_session(pool_size):
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        max_retries=3, pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    return session


def _wait_before_retry(attempt, response, range_header, retry_log, reason=""):
    delay = backoff_delay(attempt, response, RANGE_RETRY_BASE_DELAY)
    print(f"got {response.status_code} for range {range_header}{reason} "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s", file=sys.stderr)
    # list.append() is safe to call from the many concurrent walk threads
    # unprotected: CPython serializes it under the GIL, and entry order
    # within the log doesn't matter, only that every retry gets recorded.
    retry_log.append((response.status_code, attempt, delay, reason))
    response.close()
    time.sleep(delay)


def _fetch_range_attempt(session, url, range_header, attempt, retry_log):
    """Returns the response body on success, or None if `_wait_before_retry`
    already handled the backoff and the caller should try again."""
    with session.get(url, headers={"Range": range_header}, timeout=60, stream=True) as response:
        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt == MAX_RANGE_ATTEMPTS:
                response.raise_for_status()
            _wait_before_retry(attempt, response, range_header, retry_log)
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
            _wait_before_retry(attempt, response, range_header, retry_log, reason=" instead of 206")
            return None

        return response.content


def fetch_range(session, url, offset, length, retry_log):
    range_header = f"bytes={offset}-{offset + length - 1}"
    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        result = _fetch_range_attempt(session, url, range_header, attempt, retry_log)
        if result is not None:
            return result


def fetch_directory_node(session, url, header, node_offset, node_length,
                          tile_id_start, tile_id_limit, retry_log):
    """Prunes at both ends of tile-ID space, symmetric to how the upper
    (max_zoom) bound already worked before min_zoom existed: sibling entries
    in a directory are sorted and non-overlapping, so an entry's own tile_id
    is the minimum tile_id anywhere in its subtree, and the next sibling's
    tile_id (or tile_id_limit, past the last sibling) is an upper bound on
    it. That's enough to decide "entirely below tile_id_start" or "entirely
    at/above tile_id_limit" without ever fetching the subtree, so min_zoom
    shrinks the walk itself instead of just filtering its result."""
    data = fetch_range(session, url, node_offset, node_length, retry_log)
    directory = deserialize_directory(data)
    terminal_entries = []
    child_pointers = []
    for index, entry in enumerate(directory):
        if entry.tile_id >= tile_id_limit:
            break
        if entry.run_length == 0:
            next_tile_id = (directory[index + 1].tile_id if index + 1 < len(directory)
                             else tile_id_limit)
            if next_tile_id > tile_id_start:
                child_pointers.append((header["leaf_directory_offset"] + entry.offset, entry.length))
        elif entry.tile_id + entry.run_length > tile_id_start:
            terminal_entries.append(entry)
    return terminal_entries, child_pointers


def walk_directory_tree(session, url, header, tile_id_start, tile_id_limit, retry_log):
    """Workers return their results instead of submitting new work
    themselves, to avoid deadlocking this bounded pool via recursive
    submission."""
    entries = []
    dirs_walked = 0
    throttle = UpdateLineThrottle(WALK_LOG_INTERVAL)
    frontier = [(header["root_offset"], header["root_length"])]
    pending = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=WALK_WORKER_COUNT) as executor:
        while frontier or pending:
            while frontier and len(pending) < WALK_WORKER_COUNT:
                node_offset, node_length = frontier.pop()
                pending.add(executor.submit(
                    fetch_directory_node, session, url, header,
                    node_offset, node_length, tile_id_start, tile_id_limit, retry_log))

            done, pending = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                terminal_entries, child_pointers = future.result()
                entries.extend(terminal_entries)
                frontier.extend(child_pointers)
                dirs_walked += 1

            if throttle.due():
                print(f"walked {dirs_walked} directories, {len(entries)} entries so far "
                      f"(in flight: {len(pending)})", file=sys.stderr)

    return entries


def collect_entries(session, url, min_zoom, max_zoom, retry_log):
    """All directory entries covering min_zoom..max_zoom, in ascending
    *offset* order (see README.md "Fetching" for why offset order)."""
    header = deserialize_header(fetch_range(session, url, 0, 127, retry_log))
    tile_id_start = zxy_to_tileid(min_zoom, 0, 0) if min_zoom > 0 else 0
    tile_id_limit = zxy_to_tileid(max_zoom + 1, 0, 0)
    entries = walk_directory_tree(session, url, header, tile_id_start, tile_id_limit, retry_log)
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
    blocks of about total_length/W *bytes* each, not entry_count/W
    *entries* each: entries vary a lot in size (e.g. a dense city tile vs.
    a small rural one), and both fetch time (one Range GET spanning a
    worker's whole block) and transform time correlate far more closely
    with bytes than with entry count. It's only "about" because a whole
    run of same-offset entries always goes to one worker, even past the
    target size (see README.md "Fetching")."""
    entry_count = len(entries)
    total_length = sum(entry.length for entry in entries)
    blocks = [[] for _ in range(worker_count)]
    if entry_count == 0:
        return blocks
    worker = 0
    index = 0
    cumulative_length = 0
    while index < entry_count:
        run_end = index + 1
        while run_end < entry_count and entries[run_end].offset == entries[index].offset:
            run_end += 1
        run = entries[index:run_end]
        blocks[worker].extend(run)
        cumulative_length += sum(entry.length for entry in run)
        index = run_end
        target_bytes = total_length * (worker + 1) // worker_count
        if cumulative_length >= target_bytes and worker < worker_count - 1:
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


def write_retry_summary(retry_log):
    """Mirrors build_shard.py's write_retry_summary(): emitted as
    ::warning:: workflow commands so a run that hit the OpenFreeMap CDN's
    cold-cache stampede shows up as an actual warning status in the Actions
    UI, not just buried in the job summary."""
    for status_code, attempt, delay, reason in retry_log:
        print(f"::warning title=prepare-shards retry::got HTTP {status_code}{reason} on attempt "
              f"{attempt}/{MAX_RANGE_ATTEMPTS}, retried after {delay:.0f}s")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-count", type=int, default=64)
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
    retry_log = []

    # resolve source archive
    resolved = resolve_source(args.source, args.source_url).resolve()
    url, timestamp = resolved.url, resolved.build
    print(f"source={url} (build {timestamp})", file=sys.stderr)

    # one-time directory walk
    session = make_session(WALK_WORKER_COUNT)
    header, entries = collect_entries(session, url, args.min_zoom, args.max_zoom, retry_log)
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

    write_retry_summary(retry_log)


if __name__ == "__main__":
    main()
