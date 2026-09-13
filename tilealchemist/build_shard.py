#!/usr/bin/env python3
"""One worker's shard of a profile's output layer (see docs/ARCHITECTURE.md's
"Fetching" and "Parallelism" sections for what this does and why; see
docs/PROFILES.md for what a --profile actually computes per tile).

This file is the entry point only: argument parsing, then handing off. The
actual worker run (what runs, in what order, and what gets printed) lives
in `tilealchemist/shard_worker.py` (see its docstring for the phase-by-phase
flow); the two halves that drives are `tilealchemist/transform.py` (fetched
bytes -> each profile's output tiles) and `tilealchemist/mbtiles.py` (those
tiles -> one shard file per profile).

Logging to stderr is two kinds of line: major ones (phase transitions and
the final summary) that always print, and throttled `update: ...` ones that
exist only so a step taking a while doesn't look stuck. See
docs/ARCHITECTURE.md's "Worker logging" for both, and --report-interval /
--download-report-interval below for the intervals.

    tilealchemist-build-shard --worker-index 0 --profile ./my_profile.py \
        --manifest manifests/worker-000.bin --source manifests/source.json \
        --out my-profile-shard-0.mbtiles

    tilealchemist-build-shard --worker-index 0 \
        --profile ./my_profile.py,./other_profile.py \
        --manifest manifests/worker-000.bin --source manifests/source.json \
        --out my-profile-shard-0.mbtiles,other-profile-shard-0.mbtiles
"""
import argparse
import os

from tilealchemist.fetch_batching import DEFAULT_MAX_FETCH_GAP
from tilealchemist.profiles import load_profile
from tilealchemist.schemas import SCHEMAS
from tilealchemist.shard_worker import run_worker
from tilealchemist.transform import DEFAULT_REPORT_INTERVAL


# Default for --download-report-interval: its own flag, and shorter than the
# transform's, because the download phase it covers is itself shorter (see
# docs/ARCHITECTURE.md "Worker logging").
DEFAULT_DOWNLOAD_REPORT_INTERVAL = 15.0


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-index", type=int, required=True)
    parser.add_argument("--manifest", required=True,
                         help="this worker's manifest file from prepare_shards.py")
    parser.add_argument("--source", required=True, help="source.json written by prepare_shards.py")
    parser.add_argument("--out", required=True,
                         help="comma-separated output mbtiles path(s), one per --profile, "
                              "matched by position")
    parser.add_argument("--profile", required=True,
                         help="comma-separated path(s) to a profile's .py file to apply, e.g. "
                              "\"./my_profile.py\" or "
                              "\"./my_profile.py,./other_profile.py\"")
    parser.add_argument("--schema", choices=sorted(SCHEMAS), default="openmaptiles",
                         help="which source tile schema to read (default openmaptiles)")
    parser.add_argument("--report-interval", type=float, default=DEFAULT_REPORT_INTERVAL,
                         help="seconds between throttled transform-progress updates (default 60; "
                              "download-progress updates have their own "
                              "--download-report-interval)")
    parser.add_argument("--download-report-interval", type=float,
                         default=DEFAULT_DOWNLOAD_REPORT_INTERVAL,
                         help="seconds between throttled download-progress updates (default 15, "
                              "shorter than --report-interval because the download phase it "
                              "covers is itself shorter)")
    parser.add_argument("--max-fetch-gap", type=int, default=DEFAULT_MAX_FETCH_GAP,
                         help="how many bytes of archive that this worker does not read may sit "
                              "between two of its entries before the batch is split into another "
                              f"range request (default {DEFAULT_MAX_FETCH_GAP}); such gaps come "
                              "from PMTiles dedup, and get wide at a high --min-zoom")
    parser.add_argument("--transform-workers", type=int, default=os.cpu_count() or 1,
                         help="parallel processes for the CPU-bound transform phase "
                              "(default: all available cores; 1 disables pooling and runs "
                              "inline, same as before this flag existed)")
    args = parser.parse_args()

    if args.max_fetch_gap < 0:
        parser.error(f"--max-fetch-gap ({args.max_fetch_gap}) must not be negative")

    profile_paths = args.profile.split(",")
    out_paths = args.out.split(",")
    if len(profile_paths) != len(out_paths):
        parser.error(f"--profile has {len(profile_paths)} entries but --out has {len(out_paths)}; "
                      f"they must match 1:1")
    profile_classes = []
    for path in profile_paths:
        try:
            profile_classes.append(load_profile(path))
        except ValueError as error:
            parser.error(str(error))

    args.profile = profile_paths
    args.profile_classes = profile_classes
    args.out = out_paths

    return args


def main():
    run_worker(parse_args())


if __name__ == "__main__":
    main()
