#!/usr/bin/env python3
"""CLI entry point for the planning step; the run itself is in shard_prep.py."""
import argparse

from tilealchemist.schemas import SchemaName
from tilealchemist.shard_prep import run_prepare
from tilealchemist.sources import SOURCES, resolve_source
from tilealchemist.zoom import MAX_SUPPORTED_ZOOM, ZoomLevel

HELP = """The whole run's one-time planning step, before any shard worker starts.

Walks a source PMTiles archive's directory tree once and partitions the
resulting entries, plus computed gaps, into --worker-count contiguous
manifests, one per worker. docs/ARCHITECTURE.md ("Fetching") says why it is
structured this way.

Prints the layer's attribution on stdout; every log line goes to stderr.

    tilealchemist-prepare-shards --worker-count 128 --min-zoom 0 --max-zoom 14 \\
        --out-dir manifests/
"""


def zoom_level_type(value):
    zoom = int(value)  # A non-numeric value is argparse's own error to report.
    try:
        return ZoomLevel(zoom)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_SUPPORTED_ZOOM}") from None


def schema_type(value):
    try:
        return SchemaName(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be one of {', '.join(SchemaName)}") from None


def parse_args():
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-count", type=int, default=128)
    parser.add_argument("--min-zoom", type=zoom_level_type, default=ZoomLevel.Z0)
    parser.add_argument("--max-zoom", type=zoom_level_type, default=ZoomLevel.Z14)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--attribution", default=None,
                         help="what the built layer credits, as a template in which "
                              "`{source}` stands for the attribution the archive declares "
                              "for itself; left off, the archive's own is carried through "
                              "unchanged")
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
    # Discarded: built only to make a bad flag combination a usage error. Touches no network.
    try:
        resolve_source(args.source, args.source_url, args.schema)
    except ValueError as error:
        parser.error(str(error))
    return args


def main():
    run_prepare(parse_args())


if __name__ == "__main__":
    main()
