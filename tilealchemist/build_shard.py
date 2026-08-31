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

    tilealchemist-build-shard --worker-index 0 --profile tilealchemist/profiles/land.py \
        --manifest manifests/worker-000.bin --source manifests/source.json \
        --out land-shard-0.mbtiles

    tilealchemist-build-shard --worker-index 0 \
        --profile tilealchemist/profiles/land.py,tilealchemist/profiles/cropped_waterways.py \
        --manifest manifests/worker-000.bin --source manifests/source.json \
        --out land-shard-0.mbtiles,cropped-waterways-shard-0.mbtiles
"""
import argparse
import bisect
import concurrent.futures
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
from tilealchemist.profiles import load_profile
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
    def __init__(self, total_bytes, interval):
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
            print(f"update: downloading tile data: "
                  f"{downloaded}/{self.total_bytes} bytes ({pct:.1f}%), "
                  f"around tile z{zoom}/x{tile_column}/y{tile_row}",
                  file=sys.stderr)


class TransformProgress:
    def __init__(self, total_entries, interval):
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
            print(f"update: transforming tiles: "
                  f"{processed}/{self.total_entries} ({pct:.1f}%), "
                  f"currently around tile z{zoom}/x{tile_column}/y{tile_row}",
                  file=sys.stderr)


def estimate_tile_at_offset(batch_entries_by_offset, offsets, position):
    """Rough "current tile" estimate: the last entry whose offset is <= `position`."""
    index = bisect.bisect_right(offsets, position) - 1
    index = max(0, min(index, len(batch_entries_by_offset) - 1))
    return tileid_to_zxy(batch_entries_by_offset[index].tile_id)


# Retries for transient, non-fatal failure modes talking to the CDN, mostly
# seen under many concurrent workers hitting a freshly-published archive at
# once (cold-cache stampede): a full 200 instead of a 206 (server ignored
# the Range header), a 429/5xx (server rate-limiting or buckling under the
# burst), or the connection dropping mid-stream on a batch large enough to
# take a while to download. None of these is a permanent per-request
# failure, so all are worth a few backed-off retries before giving up for
# good.
MAX_RANGE_ATTEMPTS = 6
RANGE_RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _wait_before_retry(worker_index, attempt, response, reason=""):
    """Prints the retry directly as a `::warning::` workflow command (rather
    than a plain line now and a replayed `::warning::` later) so a run that
    hit the OpenFreeMap CDN's cold-cache stampede shows up as an actual
    warning status in the Actions UI as it happens, with no duplicate line."""
    delay = backoff_delay(attempt, response, RANGE_RETRY_BASE_DELAY)
    print(f"::warning title=worker {worker_index} retry::got {response.status_code}{reason} "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s", file=sys.stderr)
    response.close()
    time.sleep(delay)


def _wait_before_retry_after_stream_error(worker_index, attempt, error, downloaded):
    # No response to read a Retry-After header from (the connection already
    # broke), so back off using jitter alone.
    delay = backoff_delay(attempt, None, RANGE_RETRY_BASE_DELAY)
    print(f"::warning title=worker {worker_index} retry::connection dropped after "
          f"{downloaded} bytes ({error.__class__.__name__}) "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s", file=sys.stderr)
    time.sleep(delay)


def _fetch_batch_streaming_attempt(session, url, range_header, batch_offset, batch_entries, offsets,
                                    download_progress, attempt, worker_index, chunk_size):
    """Returns the downloaded bytes on success, or None if `_wait_before_retry`
    already handled the backoff and the caller should try again."""
    with session.get(url, headers={"Range": range_header}, timeout=300, stream=True) as response:
        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt == MAX_RANGE_ATTEMPTS:
                response.raise_for_status()
            _wait_before_retry(worker_index, attempt, response)
            return None

        response.raise_for_status()
        if response.status_code != 206:
            if attempt == MAX_RANGE_ATTEMPTS:
                raise RuntimeError(
                    f"expected HTTP 206 Partial Content for ranged request ({range_header}) "
                    f"after {MAX_RANGE_ATTEMPTS} attempts, got {response.status_code}: server "
                    f"ignored the Range header and would send the entire archive instead of "
                    f"just this batch"
                )
            _wait_before_retry(worker_index, attempt, response, reason=" instead of 206")
            return None

        chunks = []
        downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                chunks.append(chunk)
                downloaded += len(chunk)
                tile = estimate_tile_at_offset(batch_entries, offsets, batch_offset + downloaded)
                download_progress.add(len(chunk), tile)
            return b"".join(chunks)
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as error:
            # The CDN connection can drop mid-stream on a batch this large
            # (seen as an IncompleteRead well past the halfway point); worth
            # a few backed-off retries before giving up, same as a bad status.
            if attempt == MAX_RANGE_ATTEMPTS:
                raise
            _wait_before_retry_after_stream_error(worker_index, attempt, error, downloaded)
            return None


def fetch_batch_streaming_with_retries(session, url, tile_data_offset, batch, download_progress,
                                        worker_index, chunk_size=1024 * 1024):
    batch_offset, batch_length, batch_entries = batch
    abs_offset = tile_data_offset + batch_offset
    offsets = [entry.offset for entry in batch_entries]
    range_header = f"bytes={abs_offset}-{abs_offset + batch_length - 1}"

    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        result = _fetch_batch_streaming_attempt(
            session, url, range_header, batch_offset, batch_entries, offsets,
            download_progress, attempt, worker_index, chunk_size)
        if result is not None:
            return result


def transform_batch_blob_multi(blob, batch, min_zoom, max_zoom, transform_progress, profiles):
    """Same dedup rationale as before this was generalized to multiple
    profiles: offset-based partitioning (see README.md "Fetching") can group
    two separate entries that dedupe to the same non-adjacent bytes into one
    worker, on top of PMTiles' own per-entry run_length dedup. Entries are
    sorted by offset, so such a pair is always adjacent here too, meaning
    comparing against the previous entry's (offset, length) is enough to
    catch it - now for every profile's output at once, computed once per
    entry instead of once per profile per entry.

    Entries outer, profiles inner: each entry's `tile_data` slice is sliced
    from `blob` once and handed to every profile's own, unchanged
    `transform_tile_bytes()` on that same object, back-to-back. This is what
    lets `mvt.decode_tile()`'s and `water.py`'s single-slot caches (both
    keyed by object identity) actually hit when two profiles read the same
    tile - if profiles were the outer loop instead, by the time a second
    profile reached this entry, unrelated entries processed in between would
    already have evicted the cached slot."""
    batch_offset, _batch_length, batch_entries = batch
    results = [[] for _ in profiles]
    previous_key = None
    previous_outputs = None
    for entry in batch_entries:
        key = (entry.offset, entry.length)
        if key == previous_key:
            outputs = previous_outputs
        else:
            tile_data = blob[entry.offset - batch_offset:
                              entry.offset - batch_offset + entry.length]
            outputs = [profile.transform_tile_bytes(tile_data) for profile in profiles]
            previous_key, previous_outputs = key, outputs
        transform_progress.tick(tileid_to_zxy(entry.tile_id))
        for run_offset in range(entry.run_length):
            zoom, tile_column, tile_row = tileid_to_zxy(entry.tile_id + run_offset)
            if min_zoom <= zoom <= max_zoom:
                for profile_results, output_data in zip(results, outputs):
                    profile_results.append((zoom, tile_column, tile_row, output_data))
    return results


class _NullProgress:
    """Stand-in for TransformProgress in a pool worker process: per-chunk
    completion is reported by the parent process instead (see run_transform()),
    since TransformProgress's throttle uses a threading.Lock, which can't be
    pickled across a process boundary at all."""

    def tick(self, tile):
        pass


# How many transform chunks to create per worker process. One chunk per
# process (today's behavior) leaves no room to rebalance: real runs showed
# four chunks with near-identical entry counts (269648/269648/269648/269645)
# finishing 2m17s, then +9m15s, then +17m9s apart, because CPU cost per tile
# tracks tile content (dense coastline vs. open ocean), not entry count.
# Submitting several smaller chunks per process lets ProcessPoolExecutor's
# own call queue hand the next pending chunk to whichever process frees up
# first, bounding the idle tail to roughly one chunk. 8 is high enough that
# the tail is ~1/8 of a process's share, low enough that per-chunk overhead
# (one profile reimport, one blob-slice pickle) stays noise against
# multi-minute chunks.
TRANSFORM_CHUNKS_PER_WORKER = 8


def _chunk_entries(real_entries, transform_workers):
    """Split real_entries into contiguous, ordered chunks of about
    len(real_entries)/(transform_workers*TRANSFORM_CHUNKS_PER_WORKER)
    *entries* each - not cumulative entry.length (compressed source bytes)
    and not cumulative entry.run_length (output tile count) either: decode
    is paid once per entry regardless of its byte length or its
    run_length, so entry count is what actually tracks how many of those
    decode calls a chunk gets stuck with. Two earlier attempts got this
    wrong in opposite directions. Balancing on bytes let one real chunk
    end up with barely 1,000 entries while a same-sized-in-bytes neighbor
    held over 170,000. Balancing on run_length made it worse: a handful of
    entries with a huge run_length "fills" a tile-sized target almost
    immediately while costing almost no decode work, so real,
    unique-content entries (run_length 1, one decode each) piled up
    into far larger chunks than their peers. Targets
    TRANSFORM_CHUNKS_PER_WORKER chunks per worker process so free
    processes can pull more work instead of idling (see run_transform()).

    Contiguity and original order are load-bearing: _blob_slice_for_chunk()
    slices one byte range per chunk, and transform_batch_blob_multi()'s
    consecutive-entry dedup only looks at the previous entry (a dedup pair
    split across a chunk boundary just misses that one dedup, which is
    harmless).

    transform_workers <= 1, or fewer than 2 entries, returns a single
    chunk, keeping --transform-workers 1 on run_transform()'s inline,
    no-pool path exactly as before. Chunk count is otherwise exactly
    ceil(len(real_entries) / target_count), bounded by
    transform_workers * TRANSFORM_CHUNKS_PER_WORKER (plus at most one
    partial remainder chunk)."""
    if transform_workers <= 1 or len(real_entries) <= 1:
        return [real_entries]
    chunk_target = transform_workers * TRANSFORM_CHUNKS_PER_WORKER
    target_count = max(1, len(real_entries) // chunk_target)
    chunks = []
    chunk = []
    for entry in real_entries:
        chunk.append(entry)
        if len(chunk) >= target_count:
            chunks.append(chunk)
            chunk = []
    if chunk:
        chunks.append(chunk)
    return chunks


def _blob_slice_for_chunk(blob, batch_offset, chunk_entries):
    """This chunk's own slice of `blob`, plus the absolute offset it starts
    at - so a pool worker can index into just its slice with the same
    `entry.offset - chunk's own batch_offset` arithmetic
    transform_batch_blob_multi() already does, without needing the full blob
    (keeps the amount of data pickled to each worker process proportional to
    that chunk, not the whole batch)."""
    chunk_offset = chunk_entries[0].offset
    end = max(entry.offset + entry.length for entry in chunk_entries)
    return blob[chunk_offset - batch_offset:end - batch_offset], chunk_offset


def _transform_chunk(profile_paths, schema_name, min_zoom, max_zoom, blob_slice, blob_slice_offset,
                      chunk_entries):
    """Runs in a pool worker process (see run_transform()). Profiles can't be
    passed in as live instances or classes: load_profile() imports each
    profile's .py file via importlib.util.spec_from_file_location() without
    registering it in sys.modules (profiles/__init__.py), so the default
    pickler used to send this call's arguments to the worker process has no
    way to reconstruct them there. Reloading from `profile_paths` here costs
    one cheap reimport per chunk, not per tile. mvt.py's/water.py's
    module-level caches need no extra setup: this is a separate process, so
    they start out fresh automatically."""
    profiles = [profile_class(SCHEMAS[schema_name])
                for profile_class in (load_profile(path) for path in profile_paths)]
    batch = (blob_slice_offset, len(blob_slice), chunk_entries)
    return transform_batch_blob_multi(blob_slice, batch, min_zoom, max_zoom, _NullProgress(), profiles)


def run_transform(blob, batch, min_zoom, max_zoom, profiles, connections, args):
    """Runs the transform phase either inline (today's behavior, and always
    used for a single chunk, including --transform-workers 1) or fanned out
    across a fixed-size process pool. Chunk count is deliberately larger
    than the process count: ProcessPoolExecutor's internal call queue is
    itself the work queue - its manager thread keeps only a couple of
    submitted calls in flight per process and hands each remaining chunk to
    whichever worker returns first, so a chunk that turns out expensive
    delays only itself rather than stranding cores that already finished.
    The CPU-bound part (decode, transform, encode) is what benefits from
    more cores; the network fetch that produced `blob` already happened
    once, sequentially, before this is called.

    Each chunk's results are written to `connections` (one per profile,
    matched by position) as soon as that chunk is ready, instead of being
    collected into one big list per profile across every chunk first: a
    worker with millions of real entries would otherwise hold its entire
    shard's transformed output in memory for the whole transform phase,
    which is exactly what ran a worker out of memory in production (see
    prepare_shards.py's partition_contiguous() and _chunk_entries() above
    for the matching balancing fix). Peak memory this way is bounded by
    however many chunks are in flight at once, not by the shard's total
    size. Returns (written, skipped) counts per profile, summed across all
    chunks."""
    batch_offset, _batch_length, real_entries = batch
    chunks = _chunk_entries(real_entries, args.transform_workers)
    profile_names = ", ".join(repr(profile.name) for profile in profiles)
    written = [0] * len(profiles)
    skipped = [0] * len(profiles)

    def record(chunk_results):
        for i, (profile_results, connection) in enumerate(zip(chunk_results, connections)):
            chunk_written, chunk_skipped = write_output_tiles(profile_results, connection)
            written[i] += chunk_written
            skipped[i] += chunk_skipped

    if len(chunks) <= 1:
        print(f"starting transform for profiles {profile_names} ({len(real_entries)} entries)",
              file=sys.stderr)
        transform_progress = TransformProgress(len(real_entries), args.report_interval)
        record(transform_batch_blob_multi(blob, batch, min_zoom, max_zoom, transform_progress, profiles))
        return written, skipped

    print(f"starting transform for profiles {profile_names} ({len(real_entries)} entries, "
          f"{len(chunks)} chunks across up to {args.transform_workers} processes)", file=sys.stderr)
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.transform_workers) as executor:
        futures = {}
        for index, chunk in enumerate(chunks):
            blob_slice, blob_slice_offset = _blob_slice_for_chunk(blob, batch_offset, chunk)
            future = executor.submit(
                _transform_chunk, args.profile, args.schema, min_zoom, max_zoom,
                blob_slice, blob_slice_offset, chunk)
            futures[future] = (index, len(chunk), len(blob_slice))
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            index, entry_count, byte_count = futures[future]
            record(future.result())
            print(f"chunk {index + 1} done ({done}/{len(chunks)} chunks, "
                  f"{entry_count} entries, {byte_count} bytes)", file=sys.stderr)
    return written, skipped


def init_mbtiles(path, min_zoom, max_zoom, profile):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    connection.execute(
        "CREATE TABLE tiles ("
        "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    connection.execute(
        "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    vector_layers_json = json.dumps(
        {"vector_layers": profile.vector_layers_json()}, separators=(",", ":"))
    connection.executemany(
        "INSERT INTO metadata (name, value) VALUES (?, ?)",
        [
            ("name", profile.mbtiles_name),
            ("format", "pbf"),
            ("minzoom", str(min_zoom)),
            ("maxzoom", str(max_zoom)),
            ("bounds", WORLD_BOUNDS),
            ("json", vector_layers_json),
        ],
    )
    connection.commit()
    return connection


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-index", type=int, required=True)
    parser.add_argument("--manifest", required=True,
                         help="this worker's manifest file from prepare_shards.py")
    parser.add_argument("--source", required=True, help="source.json written by prepare_shards.py")
    parser.add_argument("--out", required=True,
                         help="comma-separated output mbtiles path(s), one per --profile, "
                              "matched by position")
    parser.add_argument("--profile", required=True,
                         help="comma-separated path(s) to a profile's .py file to apply, e.g. "
                              "\"tilealchemist/profiles/land.py\" or "
                              "\"tilealchemist/profiles/land.py,./my_profile.py\"")
    parser.add_argument("--schema", choices=sorted(SCHEMAS), default="openmaptiles",
                         help="which source tile schema to read (default openmaptiles)")
    parser.add_argument("--report-interval", type=float, default=DEFAULT_REPORT_INTERVAL,
                         help="seconds between throttled progress updates (default 60)")
    parser.add_argument("--transform-workers", type=int, default=os.cpu_count() or 1,
                         help="parallel processes for the CPU-bound transform phase "
                              "(default: all available cores; 1 disables pooling and runs "
                              "inline, same as before this flag existed)")
    args = parser.parse_args()

    profile_paths = args.profile.split(",")
    out_paths = args.out.split(",")
    if len(profile_paths) != len(out_paths):
        parser.error(f"--profile has {len(profile_paths)} entries but --out has {len(out_paths)}; "
                      f"they must match 1:1")
    profile_classes = []
    for path in profile_paths:
        try:
            profile_classes.append(load_profile(path))
        except ValueError as error:
            parser.error(str(error))
    for path, profile_class in zip(profile_paths, profile_classes):
        compatible = profile_class.compatible_schemas
        if compatible is not None and args.schema not in compatible:
            parser.error(
                f"profile {path!r} is not compatible with schema {args.schema!r} "
                f"(compatible schemas: {', '.join(sorted(compatible))})"
            )
    args.profile = profile_paths
    args.profile_classes = profile_classes
    args.out = out_paths

    return args


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


def fetch_real_entries_blob(real_entries, args, source):
    """Single shared network fetch for this worker's real entries, reused by
    every profile in args.profile: the raw bytes don't depend on which
    profile(s) transform them afterward."""
    session = make_session()
    batch_offset = real_entries[0].offset
    batch_length = max(entry.offset + entry.length for entry in real_entries) - batch_offset
    batch = (batch_offset, batch_length, real_entries)
    print(f"fetching {len(real_entries)} entries in a single range request "
          f"({batch_length} bytes)", file=sys.stderr)

    download_progress = DownloadProgress(batch_length, args.report_interval)
    print(f"starting download ({batch_length} bytes, {len(real_entries)} entries)",
          file=sys.stderr)
    blob = fetch_batch_streaming_with_retries(
        session, source["url"], source["tile_data_offset"], batch, download_progress,
        args.worker_index)
    return blob, batch


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


def write_gap_tiles(gap_entries, connection, profile):
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
    print(f"gap tiles (no archive entry at all): "
          f"filled {written}, skipped {skipped}", file=sys.stderr)
    return written, skipped


def main():
    # Setup: CLI args, source archive info, this worker's assigned entries.
    args = parse_args()
    source = load_source(args.source)
    profiles = [profile_class(SCHEMAS[args.schema]) for profile_class in args.profile_classes]
    print(f"source={source['url']} (build {source['build']}), "
          f"profiles={', '.join(p.name for p in profiles)}", file=sys.stderr)

    entries = read_manifest(args.manifest)
    real_entries, gap_entries = split_manifest_entries(entries)
    print(f"{len(real_entries)} real entries + {len(gap_entries)} gap ranges assigned",
          file=sys.stderr)

    connections = [init_mbtiles(out, source["min_zoom"], source["max_zoom"], profile)
                    for out, profile in zip(args.out, profiles)]

    # Empty shard: nothing assigned to this worker at all.
    if not real_entries and not gap_entries:
        for connection in connections:
            connection.commit()
            connection.close()
        print(f"done: (empty shard) -> {', '.join(args.out)}", file=sys.stderr)
        return

    written = [0] * len(profiles)
    skipped = [0] * len(profiles)

    # Real entries: fetch once, then transform every profile together
    # against the same fetched bytes (see run_transform()/transform_batch_blob_multi()).
    if real_entries:
        blob, batch = fetch_real_entries_blob(real_entries, args, source)
        real_written, real_skipped = run_transform(
            blob, batch, source["min_zoom"], source["max_zoom"], profiles, connections, args)
        for i in range(len(profiles)):
            written[i] += real_written[i]
            skipped[i] += real_skipped[i]

    # Gap entries: no fetch needed, just write whatever each profile fills gaps with.
    if gap_entries:
        for i, (profile, connection) in enumerate(zip(profiles, connections)):
            written_count, skipped_count = write_gap_tiles(gap_entries, connection, profile)
            written[i] += written_count
            skipped[i] += skipped_count

    # Finalize: persist every shard and report results, one line per profile.
    for connection in connections:
        connection.commit()
        connection.close()

    for profile, out, written_count, skipped_count in zip(profiles, args.out, written, skipped):
        print(f"done: profile={profile.name} written={written_count} skipped={skipped_count} "
              f"-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
