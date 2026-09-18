#!/usr/bin/env python3
"""The whole run's one-time planning step, before any shard worker starts.

Walks a source PMTiles archive's directory tree once and partitions the
resulting entries, plus computed gaps, into `--worker-count` contiguous
manifests, one per worker. README.md ("Fetching") says why it is structured
this way; tilealchemist/sources/ resolves the archive URL.

This file is the entry point only: argument parsing, then handing off. The
run itself lives in `tilealchemist/shard_prep.py`, whose docstring has the
phase-by-phase flow. The two halves it drives are
`tilealchemist/pmtiles_index.py` (a URL -> every directory entry in the zoom
range) and `tilealchemist/partition.py` (those entries -> one block of work
per worker). `tilealchemist/manifest.py` writes the blocks out in the form
build_shard.py reads back.

    tilealchemist-prepare-shards --worker-count 128 --min-zoom 0 --max-zoom 14 \
        --out-dir manifests/
"""
import argparse

from tilealchemist.schemas import SchemaName
from tilealchemist.shard_prep import run_prepare
from tilealchemist.sources import SOURCES, resolve_source
from tilealchemist.zoom import MAX_SUPPORTED_ZOOM, ZoomLevel


def zoom_level_type(value):
    """argparse type shared by --min-zoom and --max-zoom. Both accept the
    same levels, so they share one validator, and both come out of it as the
    `ZoomLevel` member everything downstream passes around."""
    zoom = int(value)  # a non-numeric value is argparse's own error to report
    try:
        return ZoomLevel(zoom)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_SUPPORTED_ZOOM}") from None


def schema_type(value):
    """argparse type for --schema: the `SchemaName` member this name stands
    for. Everything downstream of parse_args() then handles the enum, not a
    string that may or may not be one of ours."""
    try:
        return SchemaName(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be one of {', '.join(SchemaName)}") from None


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-count", type=int, default=128)
    parser.add_argument("--min-zoom", type=zoom_level_type, default=ZoomLevel.Z0)
    parser.add_argument("--max-zoom", type=zoom_level_type, default=ZoomLevel.Z14)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source", choices=sorted(SOURCES), default="openfreemap",
                         help="where to resolve the PMTiles archive from (default openfreemap)")
    parser.add_argument("--source-url", default=None,
                         help="the PMTiles URL to use, required when --source static-url")
    parser.add_argument("--schema", type=schema_type, choices=list(SchemaName), default=None,
                         help="which schema the archive's tiles are in, required when --source "
                              "static-url and not accepted otherwise: every other source says "
                              "what its provider publishes (see sources/base.py)")
    args = parser.parse_args()

    if args.min_zoom > args.max_zoom:
        parser.error(f"--min-zoom ({args.min_zoom}) must not exceed --max-zoom ({args.max_zoom})")
    # Built here only to turn a bad --source/--source-url/--schema combination
    # into a usage error instead of a traceback out of the run. run_prepare()
    # builds the one it resolves. Touches no network.
    try:
        resolve_source(args.source, args.source_url, args.schema)
    except ValueError as error:
        parser.error(str(error))
    return args


def main():
    run_prepare(parse_args())


if __name__ == "__main__":
    main()
