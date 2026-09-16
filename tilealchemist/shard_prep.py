"""The whole run's one-time planning step, driven by already-parsed CLI
args: walks a source PMTiles archive's directory tree once and partitions
the resulting entries (plus computed gaps) into `--worker-count` contiguous
manifests, one per worker. `prepare_shards.py` owns argument parsing and the
CLI docstring; it just calls `run_prepare(args)` with the result.

    run_prepare()
      resolve_source()                sources/: which archive to read
      make_session()                  ranged_fetch.py: shared by both fetches
      collect_entries()               pmtiles_index.py: header + index in
                                      2 requests, walked in memory
      compute_gaps()                  partition.py: tile_ids no entry covers
      partition_into_worker_blocks()  partition.py: each worker's share
      write_worker_manifests()        manifest.py: worker-NNN.bin
      write_source_metadata()         manifest.py: source.json, shared

Logging to stderr is major lines only: one per phase, no throttled updates
of its own; the two phases that can take a while (the index download and
the directory walk) print their own `update: ...` lines from
pmtiles_index.py. See docs/ARCHITECTURE.md's "Worker logging" for both
kinds.
"""
import os
import sys

from tilealchemist.manifest import write_source_metadata, write_worker_manifests
from tilealchemist.partition import compute_gaps, partition_into_worker_blocks
from tilealchemist.pmtiles_index import collect_entries
from tilealchemist.ranged_fetch import make_session
from tilealchemist.sources import resolve_source


def run_prepare(args):
    os.makedirs(args.out_dir, exist_ok=True)

    resolved_source = resolve_source(args.source, args.source_url, args.schema).resolve()
    print(f"source={resolved_source.url} (build {resolved_source.build}, "
          f"schema {resolved_source.schema.name})", file=sys.stderr)

    header, entries = collect_entries(make_session(), resolved_source.url,
                                       args.min_zoom, args.max_zoom)
    print(f"directory walk found {len(entries)} distinct tile entries "
          f"(min_zoom={args.min_zoom}, max_zoom={args.max_zoom})", file=sys.stderr)

    # Gaps: tile_ids the archive has no entry for at all, which the workers
    # fill from each profile's transform_gap() instead of fetching (see
    # partition.py's compute_gaps() and shard_worker.py's gap entries).
    gaps = compute_gaps(entries, args.min_zoom, args.max_zoom)
    gap_tile_count = sum(gap.run_length for gap in gaps)
    print(f"{len(gaps)} gap ranges covering {gap_tile_count} tiles with no archive "
          f"entry at all", file=sys.stderr)

    blocks = partition_into_worker_blocks(entries, gaps, args.worker_count)
    write_worker_manifests(args.out_dir, blocks)
    write_source_metadata(args.out_dir, resolved_source, args.min_zoom, args.max_zoom,
                          header["tile_data_offset"])

    non_empty_count = sum(1 for block in blocks if block)
    print(f"wrote {len(blocks)} manifests to {args.out_dir} "
          f"({non_empty_count} non-empty)", file=sys.stderr)
