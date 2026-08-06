# Architecture

How the pipeline works *around* a profile: resolving the source archive,
fetching it, sharding the work across workers, and publishing the result.
For what a profile actually computes, see
[`docs/PROFILES.md`](PROFILES.md) (the system) and
[`docs/EXAMPLE_PROFILES.md`](EXAMPLE_PROFILES.md) (today's two examples).

## Source resolution

`Source.resolve()` (`tilealchemist/sources/`) finds the PMTiles archive URL
to read from, independent of which profile is running.
`OpenFreeMapSource.resolve()` (the default) re-resolves on every run:
`files.txt` is scanned for the newest `areas/planet/<timestamp>_pt/`
directory that has **both** a `done` marker and a `tiles.pmtiles` file (the
very newest directory listed isn't necessarily done converting yet).
`StaticUrlSource` (`--source static-url --source-url ...`) skips that
resolution for any other PMTiles provider that just publishes one file at a
stable location.

## Fetching: directory-driven, not one request per tile

A naive implementation would issue one HTTP range request per tile: at
z0..z14 that's ~358M individual requests, too much load for a single free
community-run server. Instead:

1. `tilealchemist/prepare_shards.py` walks the PMTiles directory tree
   (root + leaf directories) up to `max_zoom` **once, for the whole run**,
   not once per worker, yielding every tile's
   `(tile_id, offset, length, run_length)`. Depends only on the resolved
   `Source`, never touches tile content, so it's identical regardless of
   which `Profile` runs later.
2. It sorts entries by *offset* (not tile-ID) and splits them into
   `WORKER_COUNT` **contiguous** chunks, one per worker
   (`tilealchemist/manifest.py`). Offset order tracks tile-ID order almost
   everywhere, but also catches what tile-ID order misses: an entry that
   dedupes against a *non-adjacent* tile with identical bytes (e.g. the
   same "all water" tile recurring across different oceans) lands in the
   same worker as the tile it's deduped against, instead of a random other
   worker re-fetching the same bytes. `partition_contiguous()` never splits
   a run of same-offset entries across two workers, even past a worker's
   target size.
3. Each `tilealchemist/build_shard.py` worker reads only its own manifest
   and fetches it with a single range GET spanning its first entry's
   offset to its last entry's end, reliable since the manifest is already
   a contiguous slice of offset order.
4. Reuses PMTiles' own deduplication: byte-identical tiles (e.g. a long run
   of open-ocean tiles) share one directory entry with a `run_length`,
   decoded and transformed once. Non-adjacent duplicates (step 2) show up
   as two separate entries at the same `(offset, length)`, landing next to
   each other once sorted, so `build_shard.py` memoizes on
   `(offset, length)` within a batch instead of re-decoding.
5. Accounts for *gaps*: tile-IDs with no directory entry at all (OpenFreeMap
   only stores a tile if it has something to render, so large empty
   stretches like desert or ice sheet interiors are simply absent).
   `prepare_shards.py` finds and chunks these, tagged with a sentinel
   `length=0` so `build_shard.py` calls
   `Profile.gap_tile_bytes(zoom, tile_column, tile_row)` for each one
   instead of fetching anything (see `write_gap_tiles()`).

Workers log download/transform progress to stderr, throttled to once every
60s (`--report-interval`); phase start/end lines always print regardless.

## Parallelism

Splitting the work into many small per-worker jobs rather than fewer big
ones costs almost nothing (`pip install .` is seconds), and keeps each job
far from GitHub Actions' 6-hour per-job runtime limit, plus smaller,
failure-isolated jobs and fewer, smaller range requests per worker. GitHub
also queues more than ~20 concurrently *running* jobs on a public repo, so
a higher `WORKER_COUNT` doesn't add parallelism at any one moment, just
keeps each job smaller. Each worker writes its own small mbtiles shard; a
final job merges all shards with `tile-join` into one `.pmtiles` file.

## Publishing

`.github/workflows/_pipeline.yml` is a **reusable** workflow
(`on: workflow_call`) containing only `prepare-shards` → `build-shards` →
`merge`, parameterized by `profile`, `source`, `output_basename`, and
`attribution`. Its `merge` job produces `<output_basename>.pmtiles`,
uploads it as a workflow artifact, and stops there: it deliberately does
**not** publish anywhere, so a third-party caller (see
[`docs/PROFILES.md`](PROFILES.md)) is never forced through this repo's own
credentials.

Publishing is two more reusable workflows, both called by every
`build-<profile>.yml` caller (today: `build-land.yml`,
`build-cropped-waterways.yml`), but not equally safe to call from *outside*
this repo:

- **`_publish-release.yml`** (`pmtiles_artifact`, `output_basename`, `tag`,
  `title`, `max_zoom` inputs) downloads the artifact, splits it into
  numbered parts if needed, and publishes/replaces a fixed-tag GitHub
  Release. Safe to call cross-repo: it only uses the automatic
  `secrets.GITHUB_TOKEN` and `github.repository`/`github.run_number`,
  which always reflect the *calling* repository inside a called reusable
  workflow's job (unlike `environment:`-scoped secrets specifically, which
  are documented as not flowing from caller to callee).
- **`_publish-b2.yml`** (`pmtiles_artifact`, `output_basename` inputs)
  mirrors the artifact to Backblaze B2 under that name as the object key,
  gated behind the `b2-publish` GitHub Environment (required-reviewer
  approval before credentials are used). **Not meant to be called
  cross-repo**: a job's `environment:` secrets inside a reusable workflow
  resolve against the repository that owns the *workflow file*, not the
  caller's. Extracting this into a shared file is safe here only because
  both callers live in the same repo as the file itself; an external
  caller would resolve against *this* repo's own B2 credentials instead of
  its own. A third-party adopter should write its own publish job with its
  own `environment:`/secrets instead.

Every `publish-*` job `needs:` the job that calls `_pipeline.yml`, the one
real cross-job dependency; everything else flows through `inputs.*` or
named artifacts. A third-party repo calls `_pipeline.yml` via
`uses: foxandfeature/tilealchemist/.github/workflows/_pipeline.yml@<ref>`,
a native cross-repo capability of reusable workflows, no GitHub Marketplace
listing required.
