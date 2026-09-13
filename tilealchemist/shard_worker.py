"""One worker's shard build, driven by its already-parsed CLI args:
fetches its manifest's tile data, transforms it per profile, and writes each
profile's shard file. `build_shard.py` owns argument parsing and the CLI
docstring; it just calls `run_worker(args)` with the result.

    run_worker()
      read_source_metadata()     manifest.py: source.json from prepare_shards.py
      read_manifest()            manifest.py: this worker's entries
      split_manifest_entries()   -> real entries / gap entries
      init_mbtiles()             mbtiles.py: one connection per profile
      real entries, one batch at a time (_process_real_entries):
        plan_fetch_batches()       fetch_batching.py: one range GET per batch, split on wide gaps
        fetch_batch_blob()         fetch_batching.py: that batch's bytes
        run_transform()            transform.py: a chunk of tiles at a time
        write_output_tiles()       mbtiles.py: that chunk, then drop it
      gap entries (_process_gap_entries):
        Profile.transform_gap()    one blob for every gap tile in the run
        write_gap_tiles()          mbtiles.py: nothing to fetch
      close_connections()

Logging to stderr is two kinds of line: major ones (phase transitions and
the final summary) that always print, and throttled `update: ...` ones that
exist only so a step taking a while doesn't look stuck. See
docs/ARCHITECTURE.md's "Worker logging" for both.
"""
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
    """Gap entries (see compute_gaps() in partition.py) are tagged
    with length=0, since there's nothing to fetch for them: the profile's
    transform_gap (see profiles/base.py) is written at every
    (zoom, tile_column, tile_row) in their run instead."""
    real_entries = [entry for entry in entries if entry.length > 0]
    gap_entries = [entry for entry in entries if entry.length == 0]
    return real_entries, gap_entries


def run_worker(args):
    source = read_source_metadata(args.source)
    schema = SCHEMAS[args.schema]
    profiles = [profile_class() for profile_class in args.profile_classes]
    print(f"source={source['url']} (build {source['build']}), "
          f"profiles={', '.join(profile.name for profile in profiles)}", file=sys.stderr)

    entries = read_manifest(args.manifest)
    real_entries, gap_entries = split_manifest_entries(entries)
    print(f"{len(real_entries)} real entries + {len(gap_entries)} gap ranges assigned",
          file=sys.stderr)

    connections = [init_mbtiles(out, source["min_zoom"], source["max_zoom"], profile, schema)
                    for out, profile in zip(args.out, profiles)]

    # Empty shard: nothing assigned to this worker at all.
    if not real_entries and not gap_entries:
        close_connections(connections)
        print(f"done: (empty shard) -> {', '.join(args.out)}", file=sys.stderr)
        return

    counts = [ProfileTileCounts() for _ in profiles]

    if real_entries:
        _process_real_entries(real_entries, args, source, profiles, connections, counts)
    if gap_entries:
        _process_gap_entries(gap_entries, schema, profiles, connections, counts)

    close_connections(connections)

    for profile, out, profile_counts in zip(profiles, args.out, counts):
        print(f"done: profile={profile.name} written={profile_counts.written} "
              f"skipped={profile_counts.skipped} -> {out}", file=sys.stderr)


def _process_real_entries(real_entries, args, source, profiles, connections, counts):
    """One batch at a time, fetched once and then transformed for every
    profile together against those same bytes (see
    run_transform()/transform_batch_blob_multi()). Usually there is exactly
    one batch; see plan_fetch_batches() for what splits it."""
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
        for chunk_results in run_transform(blob, batch, source["min_zoom"],
                                            source["max_zoom"], profiles, args):
            for profile_counts, profile_results, connection in zip(
                    counts, chunk_results, connections):
                profile_counts.add(*write_output_tiles(profile_results, connection))
        # Dropped before the next batch is fetched: a worker cannot afford
        # two batches' bytes in memory at once, for the same reason it
        # writes each transformed chunk out and drops it
        # (docs/ARCHITECTURE.md "Parallelism").
        del blob


def _process_gap_entries(gap_entries, schema, profiles, connections, counts):
    """No fetch needed, and one transform_gap() per profile covers every gap
    tile in the run (see profiles/base.py), so the transform side of this
    phase is the one call below."""
    for profile_counts, profile, connection in zip(counts, profiles, connections):
        gap_data = profile.transform_gap(schema)
        profile_counts.add(*write_gap_tiles(gap_entries, connection, gap_data))
