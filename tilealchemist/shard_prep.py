"""The whole run's one-time planning step, driven by already-parsed CLI args.

Walks a source PMTiles archive's directory tree once and partitions the
resulting entries, plus computed gaps, into `--worker-count` contiguous
manifests, one per worker. `prepare_shards.py` owns argument parsing and the
CLI docstring, and calls `run_prepare(args)` with the result.

    run_prepare()
      resolve_source()                sources/: which archive to read
      make_session()                  ranged_fetch.py: shared by every fetch
      collect_entries()               pmtiles_index.py: header + index in
                                      2 requests, walked in memory
      fetch_declared_attribution()    attribution.py: what the archive credits,
                                      1 more small request
      compose_attribution()           attribution.py: what this layer credits
      compute_gaps()                  partition.py: tile_ids no entry covers
      partition_into_worker_blocks()  partition.py: each worker's share
      write_worker_manifests()        manifest.py: worker-NNN.bin
      write_source_metadata()         manifest.py: source.json, shared

stdout carries the layer's attribution, for `_pipeline.yml` to hand to
`tile-join`. Everything else goes to stderr.

Logging to stderr is major lines only, one per phase, with no throttled
updates of its own. The two phases that can take a while, the index download
and the directory walk, print their own `update: ...` lines from
pmtiles_index.py. See docs/ARCHITECTURE.md "Worker logging" for both kinds.
"""
import os
import sys

from tilealchemist.attribution import compose_attribution, fetch_declared_attribution
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

    session = make_session()
    header, entries = collect_entries(session, resolved_source.url,
                                       args.min_zoom, args.max_zoom)
    print(f"directory walk found {len(entries)} distinct tile entries "
          f"(min_zoom={args.min_zoom}, max_zoom={args.max_zoom})", file=sys.stderr)

    declared = fetch_declared_attribution(session, resolved_source.url, header)
    print(f"source attribution: {declared or 'none declared by the archive'}",
          file=sys.stderr)
    # Before the manifests are written: a run that cannot state what its output
    # credits MUST stop here, not at merge time.
    attribution = compose_attribution(declared, args.attribution)

    # Gaps are tile_ids the archive has no entry for. Workers fill them from
    # each profile's transform_gap() instead of fetching; see
    # partition.py's compute_gaps() and shard_worker.py's gap entries.
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
    print(attribution)
