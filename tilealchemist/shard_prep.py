"""The run's one-time planning step; see docs/ARCHITECTURE.md "Fetching"."""
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
    # Before the manifests: an unattributable run stops here, not at merge time.
    attribution = compose_attribution(declared, args.attribution)

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
    # stdout carries the attribution alone, for _pipeline.yml to hand to tile-join.
    print(attribution)
