#!/usr/bin/env python3
"""One worker's shard of a profile's output layer (see README.md's "Method",
"Fetching", and "Parallelism" sections for what this does and why; see
tilealchemist/profiles/ for what each --profile actually computes per tile).

Logging to stderr is split into two kinds:

- Major lines (source resolved, entries assigned, "starting download"/
  "starting transform", final written/skipped summary) always print
  unconditionally, one per phase transition, never more.
- Update lines (`worker N: update: ...`) exist only so a step that's taking
  a while doesn't look stuck: current download/transform progress, printed
  at most once per `--report-interval` seconds (default 60), and not at all
  if the step finishes before the first interval elapses. A fast worker's
  whole log is major lines only.

    tilealchemist-build-shard --worker-index 0 --profile land \
        --manifest manifests/worker-000.bin --source manifests/source.json \
        --out land-shard-0.mbtiles
"""
import argparse
import bisect
import json
import os
import sqlite3
import sys
import threading
import time

import requests
from pmtiles.tile import tileid_to_zxy

from tilealchemist.backoff import backoff_delay
from tilealchemist.manifest import read_manifest
from tilealchemist.profiles import PROFILES
from tilealchemist.schemas import SCHEMAS
from tilealchemist.throttle import UpdateLineThrottle

WORLD_BOUNDS = "-180,-85.051129,180,85.051129"

# How often (seconds) update lines (download %, current tile) are allowed to
# print, and the minimum time a step must run before its first update line
# appears at all. Major phase-transition lines always print regardless.
DEFAULT_REPORT_INTERVAL = 60.0


def make_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount("https://", adapter)
    return session


class DownloadProgress:
    def __init__(self, worker_index, total_bytes, interval):
        self.worker_index = worker_index
        self.total_bytes = total_bytes
        self.downloaded = 0
        self.throttle = UpdateLineThrottle(interval)
        self.lock = threading.Lock()

    def add(self, byte_count, tile):
        with self.lock:
            self.downloaded += byte_count
            downloaded = self.downloaded
        if self.throttle.due():
            pct = (100 * downloaded / self.total_bytes) if self.total_bytes else 100.0
            zoom, tile_column, tile_row = tile
            print(f"worker {self.worker_index}: update: downloading tile data: "
                  f"{downloaded}/{self.total_bytes} bytes ({pct:.1f}%), "
                  f"around tile z{zoom}/x{tile_column}/y{tile_row}",
                  file=sys.stderr)


class TransformProgress:
    def __init__(self, worker_index, total_entries, interval):
        self.worker_index = worker_index
        self.total_entries = total_entries
        self.processed = 0
        self.throttle = UpdateLineThrottle(interval)
        self.lock = threading.Lock()

    def tick(self, tile):
        with self.lock:
            self.processed += 1
            processed = self.processed
        if self.throttle.due():
            pct = (100 * processed / self.total_entries) if self.total_entries else 100.0
            zoom, tile_column, tile_row = tile
            print(f"worker {self.worker_index}: update: transforming tiles: "
                  f"{processed}/{self.total_entries} ({pct:.1f}%), "
                  f"currently around tile z{zoom}/x{tile_column}/y{tile_row}",
                  file=sys.stderr)


def estimate_tile_at_offset(batch_entries_by_offset, offsets, position):
    """Rough "current tile" estimate: the last entry whose offset is <= `position`."""
    index = bisect.bisect_right(offsets, position) - 1
    index = max(0, min(index, len(batch_entries_by_offset) - 1))
    return tileid_to_zxy(batch_entries_by_offset[index].tile_id)


# Retries for two transient, non-fatal response shapes from the CDN, both
# seen under many concurrent workers hitting a freshly-published archive at
# once (cold-cache stampede): a full 200 instead of a 206 (server ignored
# the Range header), or a 429/5xx (server rate-limiting or buckling under
# the burst). Neither is a permanent per-request failure, so both are worth
# a few backed-off retries before giving up for good.
MAX_RANGE_ATTEMPTS = 6
RANGE_RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _wait_before_retry(worker_index, attempt, response, retry_log, reason=""):
    delay = backoff_delay(attempt, response, RANGE_RETRY_BASE_DELAY)
    print(f"worker {worker_index}: got {response.status_code}{reason} "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s", file=sys.stderr)
    retry_log.append((response.status_code, attempt, delay))
    response.close()
    time.sleep(delay)


def _fetch_batch_streaming_attempt(session, url, range_header, batch_offset, batch_entries, offsets,
                                    download_progress, worker_index, attempt, retry_log, chunk_size):
    """Returns the downloaded bytes on success, or None if `_wait_before_retry`
    already handled the backoff and the caller should try again."""
    with session.get(url, headers={"Range": range_header}, timeout=300, stream=True) as response:
        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt == MAX_RANGE_ATTEMPTS:
                response.raise_for_status()
            _wait_before_retry(worker_index, attempt, response, retry_log)
            return None

        response.raise_for_status()
        if response.status_code != 206:
            if attempt == MAX_RANGE_ATTEMPTS:
                raise RuntimeError(
                    f"expected HTTP 206 Partial Content for ranged request ({range_header}) after "
                    f"{MAX_RANGE_ATTEMPTS} attempts, got {response.status_code} - server ignored the Range "
                    f"header and would send the entire archive instead of just this batch"
                )
            _wait_before_retry(worker_index, attempt, response, retry_log, reason=" instead of 206")
            return None

        chunks = []
        downloaded = 0
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            chunks.append(chunk)
            downloaded += len(chunk)
            tile = estimate_tile_at_offset(batch_entries, offsets, batch_offset + downloaded)
            download_progress.add(len(chunk), tile)
        return b"".join(chunks)


def fetch_batch_streaming_with_retries(session, url, tile_data_offset, batch, download_progress, worker_index,
                                        retry_log, chunk_size=1024 * 1024):
    batch_offset, batch_length, batch_entries = batch
    abs_offset = tile_data_offset + batch_offset
    offsets = [entry.offset for entry in batch_entries]
    range_header = f"bytes={abs_offset}-{abs_offset + batch_length - 1}"

    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        result = _fetch_batch_streaming_attempt(session, url, range_header, batch_offset, batch_entries, offsets,
                                                 download_progress, worker_index, attempt, retry_log, chunk_size)
        if result is not None:
            return result


def transform_batch_blob_deduped(blob, batch, max_zoom, transform_progress, profile):
    """Offset-based partitioning (see README.md "Fetching") can group two
    separate entries that dedupe to the same non-adjacent bytes into one
    worker, on top of PMTiles' own per-entry run_length dedup. Entries are
    sorted by offset, so such a pair is always adjacent here too, meaning
    comparing against the previous entry's (offset, length) is enough to
    catch it."""
    batch_offset, _batch_length, batch_entries = batch
    results = []
    previous_key = None
    previous_output_data = None
    for entry in batch_entries:
        key = (entry.offset, entry.length)
        if key == previous_key:
            output_data = previous_output_data
        else:
            tile_data = blob[entry.offset - batch_offset: entry.offset - batch_offset + entry.length]
            output_data = profile.transform_tile_bytes(tile_data)
            previous_key, previous_output_data = key, output_data
        transform_progress.tick(tileid_to_zxy(entry.tile_id))
        for run_offset in range(entry.run_length):
            zoom, tile_column, tile_row = tileid_to_zxy(entry.tile_id + run_offset)
            if zoom <= max_zoom:
                results.append((zoom, tile_column, tile_row, output_data))
    return results


def fetch_and_transform_batch(session, url, tile_data_offset, batch, max_zoom, download_progress, transform_progress,
                               worker_index, retry_log, profile):
    batch_offset, batch_length, batch_entries = batch
    print(f"worker {worker_index}: starting download "
          f"({batch_length} bytes, {len(batch_entries)} entries)", file=sys.stderr)
    blob = fetch_batch_streaming_with_retries(session, url, tile_data_offset, batch, download_progress, worker_index,
                                               retry_log)
    print(f"worker {worker_index}: starting transform ({len(batch_entries)} entries)", file=sys.stderr)
    return transform_batch_blob_deduped(blob, batch, max_zoom, transform_progress, profile)


def init_mbtiles(path, max_zoom, profile):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    connection.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    connection.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    vector_layers_json = json.dumps({"vector_layers": profile.vector_layers_json()}, separators=(",", ":"))
    connection.executemany(
        "INSERT INTO metadata (name, value) VALUES (?, ?)",
        [
            ("name", profile.mbtiles_name),
            ("format", "pbf"),
            ("minzoom", "0"),
            ("maxzoom", str(max_zoom)),
            ("bounds", WORLD_BOUNDS),
            ("json", vector_layers_json),
        ],
    )
    connection.commit()
    return connection


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-index", type=int, required=True)
    parser.add_argument("--manifest", required=True, help="this worker's manifest file from prepare_shards.py")
    parser.add_argument("--source", required=True, help="source.json written by prepare_shards.py")
    parser.add_argument("--out", required=True, help="output mbtiles path")
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True,
                         help="which per-tile transform to apply")
    parser.add_argument("--schema", choices=sorted(SCHEMAS), default="openmaptiles",
                         help="which source tile schema to read (default openmaptiles)")
    parser.add_argument("--report-interval", type=float, default=DEFAULT_REPORT_INTERVAL,
                         help="seconds between throttled progress updates (default 60)")
    return parser.parse_args()


def load_source(path):
    with open(path) as file:
        return json.load(file)


def split_manifest_entries(entries):
    """Gap entries (see compute_gaps() in prepare_shards.py) are tagged
    with length=0, since there's nothing to fetch for them: the profile's
    gap_tile_bytes() (see profiles/base.py) is called instead, once per
    (zoom, tile_column, tile_row) in their run."""
    real_entries = [entry for entry in entries if entry.length > 0]
    gap_entries = [entry for entry in entries if entry.length == 0]
    return real_entries, gap_entries


def fetch_and_transform_real_entries(real_entries, args, source, retry_log, profile):
    session = make_session()
    batch_offset = real_entries[0].offset
    batch_length = max(entry.offset + entry.length for entry in real_entries) - batch_offset
    batch = (batch_offset, batch_length, real_entries)
    print(f"worker {args.worker_index}: fetching {len(real_entries)} entries in a single range request "
          f"({batch_length} bytes)", file=sys.stderr)

    download_progress = DownloadProgress(args.worker_index, batch_length, args.report_interval)
    transform_progress = TransformProgress(args.worker_index, len(real_entries), args.report_interval)
    return fetch_and_transform_batch(session, source["url"], source["tile_data_offset"], batch, source["max_zoom"],
                                      download_progress, transform_progress, args.worker_index, retry_log, profile)


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


def write_gap_tiles(gap_entries, connection, worker_index, profile):
    """Calls profile.gap_tile_bytes() once per gap tile, with that tile's
    own (zoom, tile_column, tile_row), and writes only the ones that come
    back non-None. `rows()` is a generator so a worker holding a
    hundred-thousand-tile gap (a whole desert or ice sheet interior) never
    materializes them all as one Python list before handing them to
    sqlite3, matching how real entries are streamed in write_output_tiles's
    caller."""
    written = 0
    skipped = 0

    def rows():
        nonlocal written, skipped
        for entry in gap_entries:
            for run_offset in range(entry.run_length):
                zoom, tile_column, tile_row = tileid_to_zxy(entry.tile_id + run_offset)
                output_data = profile.gap_tile_bytes(zoom, tile_column, tile_row)
                if output_data is None:
                    skipped += 1
                    continue
                written += 1
                yield (zoom, tile_column, (2 ** zoom - 1) - tile_row, output_data)

    connection.executemany(
        "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
        rows(),
    )
    print(f"worker {worker_index}: gap tiles (no archive entry at all): "
          f"filled {written}, skipped {skipped}", file=sys.stderr)
    return written, skipped


def write_retry_summary(worker_index, retry_log):
    if not retry_log:
        return
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a") as file:
        file.write(f"### worker {worker_index}: {len(retry_log)} retry(ies) needed\n")
        for status_code, attempt, delay in retry_log:
            file.write(f"- got HTTP {status_code} instead of 206 on attempt "
                       f"{attempt}/{MAX_RANGE_ATTEMPTS}, retried after {delay:.0f}s\n")


def main():
    # Setup: CLI args, source archive info, this worker's assigned entries.
    args = parse_args()
    source = load_source(args.source)
    profile = PROFILES[args.profile](SCHEMAS[args.schema])
    print(f"worker {args.worker_index}: source={source['url']} (build {source['build']}), "
          f"profile={profile.name}", file=sys.stderr)

    entries = read_manifest(args.manifest)
    real_entries, gap_entries = split_manifest_entries(entries)
    print(f"worker {args.worker_index}: {len(real_entries)} real entries + {len(gap_entries)} gap ranges assigned",
          file=sys.stderr)

    connection = init_mbtiles(args.out, source["max_zoom"], profile)

    # Empty shard: nothing assigned to this worker at all.
    if not real_entries and not gap_entries:
        connection.commit()
        connection.close()
        print(f"worker {args.worker_index} done: written=0 skipped=0 (empty shard) -> {args.out}", file=sys.stderr)
        return

    written = skipped = 0
    retry_log = []

    # Real entries: fetch + transform + write.
    if real_entries:
        results = fetch_and_transform_real_entries(real_entries, args, source, retry_log, profile)
        batch_written, batch_skipped = write_output_tiles(results, connection)
        written += batch_written
        skipped += batch_skipped

    # Gap entries: no fetch needed, just write whatever the profile fills gaps with.
    if gap_entries:
        batch_written, batch_skipped = write_gap_tiles(gap_entries, connection, args.worker_index, profile)
        written += batch_written
        skipped += batch_skipped

    # Finalize: persist the shard and report results.
    connection.commit()
    connection.close()
    print(f"worker {args.worker_index} done: written={written} skipped={skipped} -> {args.out}", file=sys.stderr)

    write_retry_summary(args.worker_index, retry_log)


if __name__ == "__main__":
    main()
