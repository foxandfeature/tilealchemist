"""One run's directory entries -> one block of work per worker."""
import itertools
import operator

from tilealchemist.manifest import Entry
from tilealchemist.pmtiles_index import tile_id_bounds

# Caps one gap record, so a single unbroken gap cannot land wholly on one worker.
GAP_CHUNK_SIZE = 200_000


def compute_gaps(entries, min_zoom, max_zoom):
    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    gaps = []
    expected = tile_id_start
    # Entries never overlap, so sorted by tile_id their ends are non-decreasing too.
    for entry in sorted(entries, key=operator.attrgetter("tile_id")):
        if entry.tile_id > expected:
            gaps.extend(_chunk_gap(expected, entry.tile_id))
        expected = entry.tile_id + entry.run_length
    if expected < tile_id_limit:
        gaps.extend(_chunk_gap(expected, tile_id_limit))
    return gaps


def _chunk_gap(start, end):
    # length=0 is the sentinel split_manifest_entries() tells a gap by.
    return [Entry(tile_id=chunk_start, offset=0, length=0,
                  run_length=min(GAP_CHUNK_SIZE, end - chunk_start))
            for chunk_start in range(start, end, GAP_CHUNK_SIZE)]


def _share_end(record_count, worker_index, worker_count):
    return record_count * (worker_index + 1) // worker_count


def _even_share(record_count, worker_count):
    return max(1, -(-record_count // worker_count))


def _atomic_groups(records, atomic_key, share_limit):
    if atomic_key is None:
        for record in records:
            yield [record]
        return
    for _key, group in itertools.groupby(records, key=atomic_key):
        run = list(group)
        for start in range(0, len(run), share_limit):
            yield run[start:start + share_limit]


def partition_evenly(records, worker_count, atomic_key=None):
    record_count = len(records)
    groups = _atomic_groups(records, atomic_key, _even_share(record_count, worker_count))
    blocks = [[] for _ in range(worker_count)]
    worker_index = 0
    assigned_count = 0
    for group in groups:
        blocks[worker_index].extend(group)
        assigned_count += len(group)
        # A group can span several shares, and every one it covered must be skipped.
        while (worker_index < worker_count - 1
               and assigned_count >= _share_end(record_count, worker_index, worker_count)):
            worker_index += 1
    return blocks


def partition_into_worker_blocks(entries, gaps, worker_count):
    real_blocks = partition_evenly(entries, worker_count,
                                   atomic_key=operator.attrgetter("offset"))
    gap_blocks = partition_evenly(gaps, worker_count)
    return [real_block + gap_block
            for real_block, gap_block in zip(real_blocks, gap_blocks)]
