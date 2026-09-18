"""One run's directory entries -> one block of work per worker.

Pure bookkeeping over the `Entry` records `pmtiles_index.py` produced (plus
the gap records computed here): no network, no files, no CLI.
`prepare_shards.py` calls this and writes each block out with
`manifest.py`. See docs/ARCHITECTURE.md's "Fetching" section for why real
entries are split contiguously in offset order, and why gaps exist at all.

    compute_gaps()                   tile_ids no entry covers at all
      _chunk_gap()                   ... capped at GAP_CHUNK_SIZE tiles each

    partition_into_worker_blocks()   each worker's real + gap share
      partition_evenly()             by record count, twice: real entries
                                     keeping same-offset runs whole, then
                                     gap records cut exactly
"""
import itertools
import operator

from tilealchemist.manifest import Entry
from tilealchemist.pmtiles_index import tile_id_bounds

# Gaps (see compute_gaps()) are chunked to at most this many tiles per
# manifest record so a single huge unbroken gap (e.g. a whole ice sheet's
# interior) doesn't land entirely on one worker.
GAP_CHUNK_SIZE = 200_000


def compute_gaps(entries, min_zoom, max_zoom):
    """Gaps in the min_zoom..max_zoom tile-ID range that no entry covers
    (see docs/ARCHITECTURE.md "Fetching"). Directory entries never overlap
    in tile_id space, so once they're sorted by tile_id their ends are
    non-decreasing too, which is why `expected` can just be overwritten
    each iteration instead of tracked as a running max."""
    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    gaps = []
    expected = tile_id_start
    for entry in sorted(entries, key=operator.attrgetter("tile_id")):
        if entry.tile_id > expected:
            gaps.extend(_chunk_gap(expected, entry.tile_id))
        expected = entry.tile_id + entry.run_length
    if expected < tile_id_limit:
        gaps.extend(_chunk_gap(expected, tile_id_limit))
    return gaps


def _chunk_gap(start, end):
    """The [start, end) tile-ID gap as records of at most GAP_CHUNK_SIZE
    tiles each, tagged length=0, the sentinel shard_worker.py's
    split_manifest_entries() tells a gap by, there being nothing to fetch."""
    return [Entry(tile_id=chunk_start, offset=0, length=0,
                  run_length=min(GAP_CHUNK_SIZE, end - chunk_start))
            for chunk_start in range(start, end, GAP_CHUNK_SIZE)]


def _share_end(record_count, worker_index, worker_count):
    """One past the last record worker `worker_index` gets when
    `record_count` of them are split as evenly as possible across
    `worker_count` workers.

    Multiplying before dividing is what makes it "as evenly as possible":
    a remainder that doesn't divide out goes to single workers spaced
    across the whole range, one record each (13 records over 5 workers ->
    2,3,2,3,3), rather than piling onto one (-> 2,2,2,2,5). No block ends
    up more than one record bigger than another."""
    return record_count * (worker_index + 1) // worker_count


def partition_evenly(records, worker_count, atomic_key=None):
    """Splits `records` into `worker_count` blocks of about
    record_count/worker_count *records* each, record count being the unit
    that tracks decode cost, unlike bytes or output tiles, which both
    failed in production (docs/ARCHITECTURE.md, "Parallelism").

    `atomic_key` names what must not be cut in two. Real entries pass
    `offset`, keeping a same-offset run whole even past a worker's target
    size (hence "about"); gaps pass nothing and are cut exactly, their
    shared sentinel offset=0 being no reason to land in one block."""
    if atomic_key is None:
        groups = [[record] for record in records]
    else:
        groups = [list(run) for _key, run in itertools.groupby(records, key=atomic_key)]
    record_count = len(records)
    blocks = [[] for _ in range(worker_count)]
    worker_index = 0
    assigned_count = 0
    for group in groups:
        blocks[worker_index].extend(group)
        assigned_count += len(group)
        share_end = _share_end(record_count, worker_index, worker_count)
        if assigned_count >= share_end and worker_index < worker_count - 1:
            worker_index += 1
    return blocks


def partition_into_worker_blocks(entries, gaps, worker_count):
    """Each worker's full share: its contiguous slice of real entries plus
    its even slice of gap records. Partitioned in two passes because the two
    kinds of record are cut differently (see partition_evenly()'s
    `atomic_key`) but are written to one manifest per worker."""
    real_blocks = partition_evenly(entries, worker_count,
                                   atomic_key=operator.attrgetter("offset"))
    gap_blocks = partition_evenly(gaps, worker_count)
    return [real_block + gap_block
            for real_block, gap_block in zip(real_blocks, gap_blocks)]
