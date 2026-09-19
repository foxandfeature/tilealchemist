"""One worker's shard build; see docs/ARCHITECTURE.md "Parallelism"."""
import sys

from tilealchemist.fetch_batching import fetch_batch_blob, plan_fetch_batches
from tilealchemist.manifest import read_manifest, read_source_metadata
from tilealchemist.mbtiles import (
    ProfileTileCounts,
    close_connections,
    init_mbtiles,
    write_gap_tiles,
    write_output_tiles,
)
from tilealchemist.ranged_fetch import make_session
from tilealchemist.schemas import SCHEMAS
from tilealchemist.transform import run_transform


def split_manifest_entries(entries):
    # compute_gaps() tags a gap length=0, there being nothing to fetch for it.
    real_entries = [entry for entry in entries if entry.length > 0]
    gap_entries = [entry for entry in entries if entry.length == 0]
    return real_entries, gap_entries


def run_worker(args):
    source = read_source_metadata(args.source)
    # From the source, never a flag: no worker may disagree with the walk.
    schema = SCHEMAS[source.schema]
    profiles = [profile_class() for profile_class in args.profile_classes]
    print(f"source={source.url} (build {source.build}, schema {schema.name}), "
          f"profiles={', '.join(profile.name for profile in profiles)}", file=sys.stderr)

    entries = read_manifest(args.manifest)
    real_entries, gap_entries = split_manifest_entries(entries)
    print(f"{len(real_entries)} real entries + {len(gap_entries)} gap ranges assigned",
          file=sys.stderr)

    connections = [init_mbtiles(out, source.min_zoom, source.max_zoom, profile, schema)
                    for out, profile in zip(args.out, profiles)]

    if not real_entries and not gap_entries:
        close_connections(connections)
        print(f"done: (empty shard) -> {', '.join(args.out)}", file=sys.stderr)
        return

    counts = [ProfileTileCounts() for _ in profiles]

    if real_entries:
        _process_real_entries(real_entries, args, source, schema, profiles, connections, counts)
    if gap_entries:
        _process_gap_entries(gap_entries, schema, profiles, connections, counts)

    close_connections(connections)

    for profile, out, profile_counts in zip(profiles, args.out, counts):
        print(f"done: profile={profile.name} written={profile_counts.written} "
              f"skipped={profile_counts.skipped} -> {out}", file=sys.stderr)


def _process_real_entries(real_entries, args, source, schema, profiles, connections, counts):
    batches = plan_fetch_batches(real_entries, args.max_fetch_gap)
    if len(batches) > 1:
        print(f"{len(real_entries)} real entries fetched in {len(batches)} range requests "
              f"(gaps over {args.max_fetch_gap} bytes are not fetched through)",
              file=sys.stderr)
    session = make_session()
    for batch_index, batch in enumerate(batches, start=1):
        batch_label = f" {batch_index}/{len(batches)}" if len(batches) > 1 else ""
        blob = fetch_batch_blob(session, batch, batch_label, args.worker_index, source,
                                 args.download_report_interval)
        for chunk_results in run_transform(blob, batch, source.min_zoom,
                                            source.max_zoom, profiles, schema, args):
            for profile_counts, profile_results, connection in zip(
                    counts, chunk_results, connections):
                profile_counts.add(*write_output_tiles(profile_results, connection))
        # A worker cannot afford two batches' bytes at once; drop before the next fetch.
        del blob


def _process_gap_entries(gap_entries, schema, profiles, connections, counts):
    """No fetch: one transform_gap() per profile covers every gap tile in the run."""
    for profile_counts, profile, connection in zip(counts, profiles, connections):
        gap_data = profile.transform_gap(schema)
        profile_counts.add(*write_gap_tiles(gap_entries, connection, gap_data))
