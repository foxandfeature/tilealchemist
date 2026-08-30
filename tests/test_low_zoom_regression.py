#!/usr/bin/env python3
"""Real-world regression check for the decode/water-union sharing between
profiles (tilealchemist/mvt.py, tilealchemist/water.py, build_shard.py's
merged entries-outer/profiles-inner transform loop) and the transform-phase
process pool (--transform-workers).

Runs on every push/PR (see .github/workflows/test.yml) despite needing real
network access to OpenFreeMap, since it's bounded to low zoom levels (a few
minutes) rather than a full multi-hour production run. Deliberately exercises
real OSM geometry instead of synthetic fixtures, so it can catch the kind of
GEOS edge case land.py/cropped_waterways.py were already hardened against -
the sort of thing hand-built polygons are unlikely to reproduce.

Runs prepare-shards once at a low --max-zoom, then build-shard several times
against that one fetched manifest, diffing the resulting .mbtiles tile
tables for exact equality between:
  - land alone vs. land run together with cropped-waterways
  - cropped-waterways alone vs. run together with land
  - --transform-workers 1 vs. the default (process pool), for the combined run

Usage:
    python3 tests/test_low_zoom_regression.py [--max-zoom 4]
"""
import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAND = str(REPO_ROOT / "tilealchemist" / "profiles" / "land.py")
CROPPED = str(REPO_ROOT / "tilealchemist" / "profiles" / "cropped_waterways.py")


def run(cmd):
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True)


def read_tiles(mbtiles_path):
    connection = sqlite3.connect(mbtiles_path)
    try:
        rows = connection.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles").fetchall()
    finally:
        connection.close()
    return {(z, x, y): data for z, x, y, data in rows}


def build_shard(work_dir, manifest, source, profile_paths, out_names, transform_workers, label):
    out_paths = [str(work_dir / f"{label}-{name}.mbtiles") for name in out_names]
    cmd = [
        sys.executable, "-m", "tilealchemist.build_shard",
        "--worker-index", "0",
        "--manifest", str(manifest),
        "--source", str(source),
        "--profile", ",".join(profile_paths),
        "--out", ",".join(out_paths),
    ]
    if transform_workers is not None:
        cmd += ["--transform-workers", str(transform_workers)]
    start = time.monotonic()
    run(cmd)
    elapsed = time.monotonic() - start
    print(f"[{label}] took {elapsed:.1f}s", file=sys.stderr)
    return [read_tiles(path) for path in out_paths], elapsed


def assert_equal(tiles_a, tiles_b, label):
    if tiles_a != tiles_b:
        only_a = set(tiles_a) - set(tiles_b)
        only_b = set(tiles_b) - set(tiles_a)
        mismatched = {key for key in tiles_a.keys() & tiles_b.keys() if tiles_a[key] != tiles_b[key]}
        raise AssertionError(
            f"{label}: mismatch - {len(only_a)} tiles only in first, "
            f"{len(only_b)} only in second, {len(mismatched)} with different bytes")
    print(f"{label}: OK ({len(tiles_a)} tiles match)")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-zoom", type=int, default=4,
                         help="low zoom cap to keep the real fetch fast (default 4)")
    parser.add_argument("--source", default="openfreemap")
    parser.add_argument("--keep", action="store_true", help="keep the temp working directory")
    args = parser.parse_args()

    work_dir = Path(tempfile.mkdtemp(prefix="tilealchemist-low-zoom-check-"))
    print(f"working directory: {work_dir}", file=sys.stderr)
    try:
        manifests_dir = work_dir / "manifests"
        run([
            sys.executable, "-m", "tilealchemist.prepare_shards",
            "--worker-count", "1",
            "--min-zoom", "0",
            "--max-zoom", str(args.max_zoom),
            "--source", args.source,
            "--out-dir", str(manifests_dir),
        ])
        manifest = manifests_dir / "worker-000.bin"
        source = manifests_dir / "source.json"

        land_alone, _ = build_shard(work_dir, manifest, source, [LAND], ["land"], 1, "land-alone")
        cropped_alone, _ = build_shard(
            work_dir, manifest, source, [CROPPED], ["cropped"], 1, "cropped-alone")
        combined_serial, t_serial = build_shard(
            work_dir, manifest, source, [LAND, CROPPED], ["land", "cropped"], 1, "combined-serial")
        combined_pooled, t_pooled = build_shard(
            work_dir, manifest, source, [LAND, CROPPED], ["land", "cropped"], None, "combined-pooled")

        assert_equal(land_alone[0], combined_serial[0],
                     "land alone vs. land+cropped-waterways (decode/union sharing)")
        assert_equal(cropped_alone[0], combined_serial[1],
                     "cropped-waterways alone vs. land+cropped-waterways (decode/union sharing)")
        assert_equal(combined_serial[0], combined_pooled[0],
                     "land: --transform-workers 1 vs. pooled default")
        assert_equal(combined_serial[1], combined_pooled[1],
                     "cropped-waterways: --transform-workers 1 vs. pooled default")

        print(f"\ntiming: --transform-workers 1 took {t_serial:.1f}s, "
              f"pooled default took {t_pooled:.1f}s", file=sys.stderr)
        print("\nALL CHECKS PASSED")
    finally:
        if args.keep:
            print(f"kept working directory: {work_dir}", file=sys.stderr)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
