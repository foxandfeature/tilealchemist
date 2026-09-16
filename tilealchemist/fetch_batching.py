"""Grouping a worker's real manifest entries into range-GET batches, and
fetching one batch's bytes. `shard_worker.py` only needs the result, one
(offset, length, entries) batch at a time, not how the split happened; that
split is the concern kept here.
"""
import sys

from tilealchemist.ranged_fetch import DownloadProgress, fetch_range

# Default for --max-fetch-gap: the most unread archive one range GET may
# span before the manifest is split into another request instead
# (docs/ARCHITECTURE.md "Fetching" for where those unread stretches come
# from). 8 MB because below that one request still beats two: a few MB of
# unread bytes on an open, streaming connection cost less than another round
# trip against a cold CDN.
DEFAULT_MAX_FETCH_GAP = 8 * 1024 * 1024


def plan_fetch_batches(real_entries, max_fetch_gap):
    """This worker's real entries grouped into one (offset, length, entries)
    batch per range request, the shape transform.py takes, one batch at a
    time.

    They arrive sorted by offset, but sorted is not adjacent: dedup points
    an entry at whatever tile first held its bytes, so two neighbours can
    sit gigabytes apart with data this run never reads in between
    (docs/ARCHITECTURE.md, "Fetching"). One GET across such a hole would
    download all of it, so the manifest is split at every hole wider than
    `max_fetch_gap` (see DEFAULT_MAX_FETCH_GAP), never between two entries
    at the same offset, which are zero apart, so transform.py's dedup of
    non-adjacent duplicates survives untouched."""
    return [_batch(entries) for entries in _split_on_wide_holes(real_entries, max_fetch_gap)]


def _split_on_wide_holes(real_entries, max_fetch_gap):
    """The entries in runs, starting a new one at every entry more than
    `max_fetch_gap` unread bytes past the end of everything before it: a
    running end, not the previous entry's end, since an earlier long entry
    can reach past a later short one's start."""
    entries, reach = [], 0
    for entry in real_entries:
        if entries and entry.offset - reach > max_fetch_gap:
            yield entries
            entries, reach = [], 0
        entries.append(entry)
        reach = max(reach, entry.offset + entry.length)
    if entries:
        yield entries


def _batch(batch_entries):
    """One range request's worth of entries as (offset, length, entries):
    the span from the first entry's offset to the furthest end in the
    batch."""
    batch_offset = batch_entries[0].offset
    batch_length = max(entry.offset + entry.length for entry in batch_entries) - batch_offset
    return batch_offset, batch_length, batch_entries


def fetch_batch_blob(session, batch, batch_label, worker_index, source, report_interval):
    """Single shared network fetch for one batch of this worker's real
    entries, reused by every profile in the run: the raw bytes don't depend
    on which profile(s) transform them afterward. One range GET spanning the
    batch, which plan_fetch_batches() has already made worth fetching in one
    piece. `batch_label` numbers it when there is more than one."""
    batch_offset, batch_length, batch_entries = batch
    progress = DownloadProgress(batch_length, report_interval, f"tile data{batch_label}")

    print(f"starting download{batch_label} ({batch_length} bytes, {len(batch_entries)} entries "
          f"in a single range request)", file=sys.stderr)
    return fetch_range(
        session, source.url, source.tile_data_offset + batch_offset, batch_length,
        retry_label=f"worker {worker_index}", on_chunk=progress.update)
