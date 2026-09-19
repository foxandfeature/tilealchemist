"""One run's directory entries -> one block of work per worker.

Pure bookkeeping over the `Entry` records `pmtiles_index.py` produced, plus
the gap records computed here. No network, no files, no CLI.
`prepare_shards.py` calls this and writes each block out with `manifest.py`.
docs/ARCHITECTURE.md "Fetching" says why real entries are split contiguously
in offset order, and why gaps exist at all.

    compute_gaps()                   tile_ids no entry covers at all
      _chunk_gap()                   ... capped at GAP_CHUNK_SIZE tiles each

    partition_into_worker_blocks()   each worker's real + gap share
      partition_evenly()             by record count, twice: real entries
        _atomic_groups()             keeping same-offset runs whole up to
                                     one share, then gap records cut exactly
"""
import itertools
import operator

from tilealchemist.manifest import Entry
from tilealchemist.pmtiles_index import tile_id_bounds

# Gaps (see compute_gaps()) are chunked to at most this many tiles per
# manifest record. A single huge unbroken gap — a whole ice sheet's
# interior — MUST NOT land entirely on one worker.
GAP_CHUNK_SIZE = 200_000


def compute_gaps(entries, min_zoom, max_zoom):
    """Gaps in the min_zoom..max_zoom tile-ID range that no entry covers
    (docs/ARCHITECTURE.md "Fetching").

    Directory entries never overlap in tile_id space. Sorted by tile_id,
    their ends are non-decreasing too, so `expected` can be overwritten each
    iteration rather than tracked as a running max."""
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
    tiles each. length=0 is the sentinel shard_worker.py's
    split_manifest_entries() tells a gap by, there being nothing to fetch."""
    return [Entry(tile_id=chunk_start, offset=0, length=0,
                  run_length=min(GAP_CHUNK_SIZE, end - chunk_start))
            for chunk_start in range(start, end, GAP_CHUNK_SIZE)]


def _share_end(record_count, worker_index, worker_count):
    """One past the last record worker `worker_index` gets when
    `record_count` of them are split as evenly as possible across
    `worker_count` workers.

    Multiplying before dividing is what makes it "as evenly as possible".
    A remainder that does not divide out goes to single workers spaced across
    the range, one record each (13 records over 5 workers -> 2,3,2,3,3),
    rather than piling onto one (-> 2,2,2,2,5). No block ends up more than
    one record bigger than another."""
    return record_count * (worker_index + 1) // worker_count


def _even_share(record_count, worker_count):
    """The largest block an even split produces, and so the most records one
    atomic group may carry on its own; see _atomic_groups()."""
    return max(1, -(-record_count // worker_count))


def _atomic_groups(records, atomic_key, share_limit):
    """Yields `records` in the contiguous runs partition_evenly() MUST NOT
    cut, none of them longer than `share_limit`.

    A run longer than one whole share is cut into share-sized pieces, which
    costs only what keeping it whole was buying: the duplicate bytes behind
    it are fetched once per piece instead of once (docs/ARCHITECTURE.md,
    "Fetching", step 2). That is one tile's bytes per extra worker. Keeping
    such a run whole costs the opposite way, and unboundedly: a planet
    build's "all water" run is millions of entries, every one of which lands
    on a single worker no matter how high `worker_count` goes.

    A generator, not a list, because the only caller walks it once and in
    order. A planet run reaches here with ~58M entries, nearly all of them
    at a distinct offset and so in a group of their own, and holding that
    many one-record lists at once costs about 4 GB on a 16 GB runner that
    is already carrying the entries themselves."""
    if atomic_key is None:
        for record in records:
            yield [record]
        return
    for _key, group in itertools.groupby(records, key=atomic_key):
        run = list(group)
        for start in range(0, len(run), share_limit):
            yield run[start:start + share_limit]


def partition_evenly(records, worker_count, atomic_key=None):
    """Splits `records` into `worker_count` blocks of about
    record_count/worker_count *records* each.

    Record count is the unit that tracks decode cost. Bytes and output tiles
    both failed in production (docs/ARCHITECTURE.md, "Parallelism").

    `atomic_key` names what MUST NOT be cut in two. Real entries pass
    `offset`, keeping a same-offset run whole up to one worker's share, past
    which _atomic_groups() cuts it. Gaps pass nothing and are cut exactly:
    their shared sentinel offset=0 is no reason to land in one block.

    A group can still carry the current worker past more than one share end
    — its share is a target, not a cap. Advancing MUST then skip every
    worker that group already covered, or each of them would be handed the
    next single group and nothing else, however small."""
    record_count = len(records)
    groups = _atomic_groups(records, atomic_key, _even_share(record_count, worker_count))
    blocks = [[] for _ in range(worker_count)]
    worker_index = 0
    assigned_count = 0
    for group in groups:
        blocks[worker_index].extend(group)
        assigned_count += len(group)
        while (worker_index < worker_count - 1
               and assigned_count >= _share_end(record_count, worker_index, worker_count)):
            worker_index += 1
    return blocks


def partition_into_worker_blocks(entries, gaps, worker_count):
    """Each worker's full share: its contiguous slice of real entries plus
    its even slice of gap records.

    Two passes, because the two kinds of record are cut differently (see
    partition_evenly()'s `atomic_key`) but go to one manifest per worker."""
    real_blocks = partition_evenly(entries, worker_count,
                                   atomic_key=operator.attrgetter("offset"))
    gap_blocks = partition_evenly(gaps, worker_count)
    return [real_block + gap_block
            for real_block, gap_block in zip(real_blocks, gap_blocks)]
