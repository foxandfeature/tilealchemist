"""The CPU-bound half of a worker; see docs/ARCHITECTURE.md "Parallelism"."""
import collections
import concurrent.futures
import sys

from pmtiles.tile import tileid_to_zxy

from tilealchemist.profiles import load_profile
from tilealchemist.schemas import SCHEMAS
from tilealchemist.tile import Tile
from tilealchemist.throttle import UpdateLineThrottle


DEFAULT_REPORT_INTERVAL = 60.0


class TransformProgress:
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
    zoom, column, row = tileid_to_zxy(entry.tile_id)
    if entry.run_length == 1:
        return f"tile {zoom}/{column}/{row}"
    return f"tile {zoom}/{column}/{row} (run of {entry.run_length} tiles)"


def _entry_outputs(tile_data, entry, profiles, schema):
    tile = Tile.decode(tile_data, schema)
    outputs = []
    for profile in profiles:
        try:
            outputs.append(profile.transform_tile(tile))
        except Exception as error:
            raise RuntimeError(
                f"profile {profile.name!r} failed on {_describe_entry(entry)}") from error
    return outputs


def transform_batch_blob_multi(blob, batch, min_zoom, max_zoom, transform_progress, profiles,
                                schema):
    batch_offset, _batch_length, batch_entries = batch
    results = [[] for _ in profiles]
    previous_key = None
    outputs = None
    for entry in batch_entries:
        # Offset order puts duplicate bytes adjacent, so one check dedupes for every profile.
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


# Spare chunks per process, so one finishing early pulls the next instead of idling.
TRANSFORM_CHUNKS_PER_WORKER = 8


def _chunk_entries(real_entries, transform_workers):
    if transform_workers <= 1 or len(real_entries) <= 1:
        return [real_entries]
    target_count = max(1, len(real_entries) // (transform_workers * TRANSFORM_CHUNKS_PER_WORKER))
    return [real_entries[start:start + target_count]
            for start in range(0, len(real_entries), target_count)]


def _blob_slice_for_chunk(blob, batch_offset, chunk_entries):
    chunk_offset = chunk_entries[0].offset
    chunk_length = max(entry.offset + entry.length for entry in chunk_entries) - chunk_offset
    start = chunk_offset - batch_offset
    return blob[start:start + chunk_length], chunk_offset


# One picklable value; `args` cannot serve, carrying profile classes a worker cannot unpickle.
ChunkJob = collections.namedtuple(
    "ChunkJob", "profile_paths schema_name min_zoom max_zoom report_interval")


def _transform_chunk(job, blob_slice, blob_slice_offset, chunk_entries, chunk_index):
    profiles = [load_profile(path)() for path in job.profile_paths]
    batch = (blob_slice_offset, len(blob_slice), chunk_entries)
    progress = TransformProgress(len(chunk_entries), job.report_interval,
                                  label=f"transforming chunk {chunk_index + 1}")
    return transform_batch_blob_multi(blob_slice, batch, job.min_zoom, job.max_zoom, progress,
                                       profiles, SCHEMAS[job.schema_name])


def _pooled_chunk_results(blob, batch_offset, chunks, job, max_workers):
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        pending = {}
        for index, chunk in enumerate(chunks):
            blob_slice, blob_slice_offset = _blob_slice_for_chunk(blob, batch_offset, chunk)
            future = executor.submit(_transform_chunk, job, blob_slice, blob_slice_offset,
                                      chunk, index)
            pending[future] = (index, len(chunk), len(blob_slice))
        for future in concurrent.futures.as_completed(pending):
            # pop, not index: a live Future pins its chunk's output for the whole phase.
            index, entry_count, byte_count = pending.pop(future)
            yield index, entry_count, byte_count, future.result()


def run_transform(blob, batch, min_zoom, max_zoom, profiles, schema, args):
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

    job = ChunkJob(args.profile, schema.name, min_zoom, max_zoom, args.report_interval)
    completed = _pooled_chunk_results(blob, batch_offset, chunks, job, args.transform_workers)
    for done, (index, entry_count, byte_count, chunk_results) in enumerate(completed, start=1):
        yield chunk_results
        print(f"chunk {index + 1} done ({done}/{len(chunks)} chunks, "
              f"{entry_count} entries, {byte_count} bytes)", file=sys.stderr)
