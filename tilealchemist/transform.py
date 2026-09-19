"""The CPU-bound half of a worker.

Already-fetched tile bytes in, every profile's output tiles out, optionally
fanned out across the machine's cores.

Nothing here touches the network, the CLI or sqlite. `shard_worker.py` does
the fetching, decides what to run, and hands each chunk coming out of here
to `mbtiles.py`. docs/ARCHITECTURE.md "Parallelism" has the design this
implements: why chunks are balanced on entry count, why there are more
chunks than processes, why each chunk is written out and dropped as it
arrives. "Worker logging" has the two kinds of stderr line.

    run_transform()                  one shard's whole transform phase,
      _chunk_entries()               yielding a chunk at a time
      transform_batch_blob_multi()   inline path: a single chunk
      _pooled_chunk_results()        pooled path: chunks -> processes
        _transform_chunk()             ... inside one worker process
          transform_batch_blob_multi()
            _entry_outputs()           one decoded Tile, every profile
"""
import collections
import concurrent.futures
import sys

from pmtiles.tile import tileid_to_zxy

from tilealchemist.profiles import load_profile
from tilealchemist.schemas import SCHEMAS
from tilealchemist.tile import Tile
from tilealchemist.throttle import UpdateLineThrottle


# How often (seconds) transform update lines MAY print, and the minimum time
# the transform must run before its first one appears. Major phase-transition
# lines always print regardless.
DEFAULT_REPORT_INTERVAL = 60.0


class TransformProgress:
    """Throttled `update: ...` lines for one transform.

    That is either a whole shard, on the inline path, or one chunk of it: a
    pool worker builds its own, see _transform_chunk(). `label` says which,
    since several processes report into the same stderr.

    Chunks fan out across processes, never threads, so an instance is only
    ever ticked from one thread and nothing here needs to be thread-safe.
    DownloadProgress in ranged_fetch.py is the same."""

    def __init__(self, total_entries, interval, label="transforming tiles"):
        self.total_entries = total_entries
        self.label = label
        self.processed = 0
        self.throttle = UpdateLineThrottle(interval)

    def tick(self, tile):
        self.processed += 1
        if not self.throttle.due():
            return
        percent = (100 * self.processed / self.total_entries) if self.total_entries else 100.0
        zoom, tile_column, tile_row = tile
        print(f"update: {self.label}: "
              f"{self.processed}/{self.total_entries} ({percent:.1f}%), "
              f"currently around tile z{zoom}/x{tile_column}/y{tile_row}",
              file=sys.stderr)


def _describe_entry(entry):
    """`entry`'s tile as z/x/y, plus its run length when it stands for more
    than one tile.

    An entry is PMTiles' dedup of consecutive tile_ids sharing the same
    bytes, so that count tells a speck apart from a whole region. It is the
    entry's own count, before the zoom filter, hence an upper bound on the
    output tiles behind it."""
    zoom, column, row = tileid_to_zxy(entry.tile_id)
    if entry.run_length == 1:
        return f"tile {zoom}/{column}/{row}"
    return f"tile {zoom}/{column}/{row} (run of {entry.run_length} tiles)"


def _entry_outputs(tile_data, entry, profiles, schema):
    """Every profile's output for one entry's bytes, decoded once into a
    single `Tile` (see tile.py) they all share.

    The decode, and any derived value two profiles both need (a water union,
    say), is computed once per tile rather than once per profile. The `Tile`
    is dropped when this returns, bounding that sharing to one tile."""
    tile = Tile.decode(tile_data, schema)
    outputs = []
    for profile in profiles:
        try:
            outputs.append(profile.transform_tile(tile))
        except Exception as error:
            # Aborting the whole shard is the intent; see mvt.encode_tile()'s
            # on_invalid_geometry note. Catching here only buys the context
            # nothing above can supply: the exception carries just geometry,
            # and every caller sees a whole batch.
            raise RuntimeError(
                f"profile {profile.name!r} failed on {_describe_entry(entry)}") from error
    return outputs


def transform_batch_blob_multi(blob, batch, min_zoom, max_zoom, transform_progress, profiles,
                                schema):
    """Every profile's output tiles for `batch`: one list per profile,
    matched to `profiles` by position.

    Each list holds one (zoom, tile_column, tile_row, output_data) tuple per
    *output tile*. An entry with run_length > 1 yields that many, and tiles
    outside [min_zoom, max_zoom] are dropped.

    Offset-based partitioning can group duplicate bytes into one worker, on
    top of PMTiles' per-entry run_length dedup (docs/ARCHITECTURE.md
    "Fetching"). Entries arrive in offset order, so a duplicate pair is
    adjacent here. Holding the previous entry's (offset, length) and outputs
    skips the repeat for every profile at once: one check per entry rather
    than one per profile per entry."""
    batch_offset, _batch_length, batch_entries = batch
    results = [[] for _ in profiles]
    previous_key = None
    outputs = None
    for entry in batch_entries:
        key = (entry.offset, entry.length)
        if key != previous_key:
            start = entry.offset - batch_offset
            outputs = _entry_outputs(blob[start:start + entry.length], entry, profiles, schema)
            previous_key = key
        transform_progress.tick(tileid_to_zxy(entry.tile_id))
        for run_offset in range(entry.run_length):
            zoom, tile_column, tile_row = tileid_to_zxy(entry.tile_id + run_offset)
            if min_zoom <= zoom <= max_zoom:
                for profile_results, output_data in zip(results, outputs):
                    profile_results.append((zoom, tile_column, tile_row, output_data))
    return results


# How many transform chunks to create per worker process. Equal entry counts
# do not mean equal cost, so the spare chunks let a process finishing early
# pull the next one instead of idling. docs/ARCHITECTURE.md "Parallelism"
# names the run this value was measured from.
TRANSFORM_CHUNKS_PER_WORKER = 8


def _chunk_entries(real_entries, transform_workers):
    """`real_entries` split into contiguous chunks of about
    len(real_entries) / (transform_workers * TRANSFORM_CHUNKS_PER_WORKER)
    entries each, in their original order.

    Entry count is the balance, not cumulative bytes or run_length — the rule
    partition.py's partition_evenly() documents in full.

    Contiguity and order MUST hold. _blob_slice_for_chunk() slices one byte
    range per chunk, and transform_batch_blob_multi()'s dedup compares only
    against the previous entry. A duplicate pair split across a boundary
    misses that one dedup, harmlessly.

    A single chunk (`transform_workers <= 1`, or fewer than two entries)
    keeps run_transform() on its inline, no-pool path."""
    if transform_workers <= 1 or len(real_entries) <= 1:
        return [real_entries]
    target_count = max(1, len(real_entries) // (transform_workers * TRANSFORM_CHUNKS_PER_WORKER))
    return [real_entries[start:start + target_count]
            for start in range(0, len(real_entries), target_count)]


def _blob_slice_for_chunk(blob, batch_offset, chunk_entries):
    """This chunk's own bytes out of `blob`, plus the absolute offset they
    start at.

    A pool worker gets only its share pickled to it, and indexes into that
    slice with the same `entry.offset - batch_offset` arithmetic
    transform_batch_blob_multi() applies to a full batch."""
    chunk_offset = chunk_entries[0].offset
    chunk_length = max(entry.offset + entry.length for entry in chunk_entries) - chunk_offset
    start = chunk_offset - batch_offset
    return blob[start:start + chunk_length], chunk_offset


# What every chunk of one run needs, as one picklable value. `args` cannot
# take this role: parse_args() hangs the loaded profile classes on it, and
# those are precisely what a worker process cannot unpickle.
ChunkJob = collections.namedtuple(
    "ChunkJob", "profile_paths schema_name min_zoom max_zoom report_interval")


def _transform_chunk(job, blob_slice, blob_slice_offset, chunk_entries, chunk_index):
    """Runs in a pool worker process; see run_transform().

    Two things it needs cannot cross a process boundary and are rebuilt here.
    The profiles, which the pickler cannot reconstruct in a worker at all
    (docs/ARCHITECTURE.md "Parallelism"), are reimported once per chunk, not
    per tile. Its TransformProgress holds a threading.Lock in its throttle.

    Only that object is unpicklable, not the reporting. The interval is a
    plain float, so a worker throttles its own lines over its own chunk,
    hence the label, while the parent's "chunk N done" lines carry the
    whole-shard view."""
    profiles = [load_profile(path)() for path in job.profile_paths]
    batch = (blob_slice_offset, len(blob_slice), chunk_entries)
    progress = TransformProgress(len(chunk_entries), job.report_interval,
                                  label=f"transforming chunk {chunk_index + 1}")
    return transform_batch_blob_multi(blob_slice, batch, job.min_zoom, job.max_zoom, progress,
                                       profiles, SCHEMAS[job.schema_name])


def _pooled_chunk_results(blob, batch_offset, chunks, job, max_workers):
    """Yields (chunk index, entry count, byte count, that chunk's results) as
    each chunk finishes, in completion order.

    Every chunk is submitted up front. ProcessPoolExecutor's call queue is
    then the work queue, handing each pending chunk to whichever process
    returns first — the mechanism TRANSFORM_CHUNKS_PER_WORKER's spare chunks
    exist for. Yielding on completion, rather than returning a list, lets
    shard_worker.py drop each chunk as it goes; see run_transform().

    Dropping it takes both holders letting go. A `Future` keeps the result
    it was handed for as long as the `Future` itself is alive, so `pending`
    MUST give up its entry as that chunk is yielded: holding all of them
    until the pool shuts down would pin every chunk's output for the whole
    transform phase, which is the memory run_transform() yields per chunk to
    avoid in the first place."""
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        pending = {}
        for index, chunk in enumerate(chunks):
            blob_slice, blob_slice_offset = _blob_slice_for_chunk(blob, batch_offset, chunk)
            future = executor.submit(_transform_chunk, job, blob_slice, blob_slice_offset,
                                      chunk, index)
            pending[future] = (index, len(chunk), len(blob_slice))
        for future in concurrent.futures.as_completed(pending):
            index, entry_count, byte_count = pending.pop(future)
            yield index, entry_count, byte_count, future.result()


def run_transform(blob, batch, min_zoom, max_zoom, profiles, schema, args):
    """Runs the transform phase, yielding one chunk's results at a time.

    Each yield is a list of per-profile result lists, matched to `profiles`
    by position, in the shape write_output_tiles() (mbtiles.py) takes. The
    CPU-bound work — decode, transform, encode — is what fans out across
    cores; `blob` was fetched once, sequentially, before this was called.
    `schema` is the one the run's source.json named (see shard_worker.py).
    `args` is the worker's parsed command line, for the three flags this
    phase reads: --transform-workers, --profile and --report-interval.

    A single chunk, always the case under `--transform-workers 1`, runs
    inline rather than paying a process for one call. Progress does not hinge
    on that: every transform throttles its own `update: ...` lines (see
    TransformProgress), over the whole shard inline or over one chunk in a
    worker. Only the parent knows where the run as a whole stands, which is
    what the "chunk N done" line below reports.

    Yielding per chunk lets shard_worker.py write each out and drop it.
    Holding a whole shard's transformed output ran a worker out of memory in
    production (docs/ARCHITECTURE.md "Parallelism")."""
    batch_offset, _batch_length, real_entries = batch
    chunks = _chunk_entries(real_entries, args.transform_workers)

    fanout = (f", {len(chunks)} chunks across up to {args.transform_workers} processes"
              if len(chunks) > 1 else "")
    print(f"starting transform for profiles "
          f"{', '.join(repr(profile.name) for profile in profiles)} "
          f"({len(real_entries)} entries{fanout})", file=sys.stderr)

    if len(chunks) <= 1:
        transform_progress = TransformProgress(len(real_entries), args.report_interval)
        yield transform_batch_blob_multi(blob, batch, min_zoom, max_zoom, transform_progress,
                                         profiles, schema)
        return

    # The schema crosses into a pool worker as its SchemaName, not as the
    # object. The child looks the same singleton up out of SCHEMAS; see
    # _transform_chunk().
    job = ChunkJob(args.profile, schema.name, min_zoom, max_zoom, args.report_interval)
    completed = _pooled_chunk_results(blob, batch_offset, chunks, job, args.transform_workers)
    for done, (index, entry_count, byte_count, chunk_results) in enumerate(completed, start=1):
        yield chunk_results
        print(f"chunk {index + 1} done ({done}/{len(chunks)} chunks, "
              f"{entry_count} entries, {byte_count} bytes)", file=sys.stderr)
