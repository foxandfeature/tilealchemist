#!/usr/bin/env python3
"""The whole run's one-time planning step, before any shard worker starts:
walks a source PMTiles archive's directory tree once and partitions the
resulting entries (plus computed gaps) into `--worker-count` contiguous
manifests, one per worker. See README.md ("Fetching") for why it's
structured this way, and tilealchemist/sources/ for how the archive URL
itself gets resolved.

This file is the entry point only: argument parsing, then handing off. The
actual run (what runs, in what order, and what gets printed) lives in
`tilealchemist/shard_prep.py` (see its docstring for the phase-by-phase
flow); the two halves that drives are `tilealchemist/pmtiles_index.py` (a
URL -> every directory entry in the zoom range) and `tilealchemist/partition.py`
(those entries -> one block of work per worker), with `tilealchemist/manifest.py`
writing the blocks out in the form build_shard.py reads back.

    tilealchemist-prepare-shards --worker-count 128 --min-zoom 0 --max-zoom 14 \
        --out-dir manifests/
"""
import argparse

from tilealchemist.pmtiles_index import MAX_SUPPORTED_ZOOM
from tilealchemist.schemas import SCHEMAS
from tilealchemist.shard_prep import run_prepare
from tilealchemist.sources import SOURCES, resolve_source


def zoom_level_type(value):
    """argparse type shared by --min-zoom and --max-zoom: both accept
    exactly the same range, so they get exactly one validator."""
    zoom = int(value)
    if not (0 <= zoom <= MAX_SUPPORTED_ZOOM):
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_SUPPORTED_ZOOM}")
    return zoom


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-count", type=int, default=128)
    parser.add_argument("--min-zoom", type=zoom_level_type, default=0)
    parser.add_argument("--max-zoom", type=zoom_level_type, default=14)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source", choices=sorted(SOURCES), default="openfreemap",
                         help="where to resolve the PMTiles archive from (default openfreemap)")
    parser.add_argument("--source-url", default=None,
                         help="the PMTiles URL to use, required when --source static-url")
    parser.add_argument("--schema", choices=sorted(SCHEMAS), default=None,
                         help="which schema the archive's tiles are in, required when --source "
                              "static-url and not accepted otherwise: every other source says "
                              "what its provider publishes (see sources/base.py)")
    args = parser.parse_args()

    if args.min_zoom > args.max_zoom:
        parser.error(f"--min-zoom ({args.min_zoom}) must not exceed --max-zoom ({args.max_zoom})")
    # Built here only to turn a bad --source/--source-url/--schema combination
    # into a usage error instead of a traceback out of the run; run_prepare()
    # builds the one it actually resolves. Costs nothing, touching no network.
    try:
        resolve_source(args.source, args.source_url, args.schema)
    except ValueError as error:
        parser.error(str(error))
    return args


def main():
    run_prepare(parse_args())


if __name__ == "__main__":
    main()
