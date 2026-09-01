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
   (root + leaf directories) between `min_zoom` (default 0) and `max_zoom`
   **once, for the whole run**, not once per worker, yielding every tile's
   `(tile_id, offset, length, run_length)`. Depends only on the resolved
   `Source`, never touches tile content, so it's identical regardless of
   which `Profile` runs later. Both bounds prune the walk itself, not just
   its result: sibling entries in a directory are sorted and non-overlapping
   tile-ID ranges, so a child pointer whose whole range falls outside
   `[min_zoom, max_zoom]` is never even decoded, the same way `max_zoom`
   already skipped subtrees entirely past it before `min_zoom` existed (see
   `walk_directory_tree()`). The walk itself issues no requests at all: the
   whole index (root directory + metadata + every leaf directory) is laid
   out contiguously by PMTiles writers and its extent is known from the
   127-byte header, so `collect_entries()` pulls it down in a single Range
   request and walks it from memory — two requests for the entire run,
   however deep the tree.
2. It sorts entries by *offset* (not tile-ID) and splits them into
   `--worker-count` (the reusable pipeline's `worker_count` input, default
   128) **contiguous** chunks, one per worker (`tilealchemist/manifest.py`).
   Offset order tracks tile-ID order almost
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
   a contiguous slice of offset order. When a worker is building multiple
   profiles in one invocation (a comma-separated `--profile` list, see
   "Publishing" below), it still does exactly one range GET for the batch
   and reuses those same fetched bytes for every profile's transform,
   instead of fetching once per profile.
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
   `length=0` so `build_shard.py` writes `Profile.gap_tile_bytes` at each
   one's coordinates instead of fetching anything (see `write_gap_tiles()`).

Workers log download/transform progress to stderr, throttled to once every
60s (`--report-interval`); phase start/end lines always print regardless.

## Parallelism

Splitting the work into many small per-worker jobs rather than fewer big
ones costs almost nothing (`pip install .` is seconds), and keeps each job
far from GitHub Actions' 6-hour per-job runtime limit, plus smaller,
failure-isolated jobs and fewer, smaller range requests per worker. GitHub
also queues more than ~20 concurrently *running* jobs on a public repo, so
a higher `worker_count` doesn't add parallelism at any one moment, just
keeps each job smaller: that's why the default is 128, not higher. Each
worker writes its own small mbtiles shard; a final job merges all shards
with `tile-join` into one `.pmtiles` file.

A worker building multiple profiles in one run (`_pipeline.yml` called with
a comma-separated `profile`) still does exactly one fetch, so per-worker
*network* time is unchanged. Per-worker *CPU* time no longer simply scales
with the number of profiles built together the way it used to: `build_shard.py`
now decodes each unique tile once and hands the same decoded tile to every
profile back-to-back (entries outer, profiles inner in
`transform_batch_blob_multi()`), and `mvt.decode_tile()`/`water.py`'s
`surface_water_union()` each memoize their own last call by object identity,
so a second profile reading the same tile right after the first is a cache
hit rather than repeated gunzip+protobuf decode or polygon-union work. Two
profiles that don't share any of that underlying work (a hypothetical
buildings profile alongside `land`, say) still each pay their own cost in
full - only genuinely-shared steps (decode, and `land`/`cropped-waterways`'s
own shared water union) got cheaper.

Independently of that, the transform phase itself (decode, transform,
encode, sqlite insert - all CPU-bound, not network) can now fan out across a
worker's own CPU cores via `--transform-workers` (default: all available
cores; `1` disables pooling and matches the old single-process behavior
exactly). `real_entries` is split into contiguous chunks sized by **entry count**,
deliberately producing several times more chunks than there are processes
(`TRANSFORM_CHUNKS_PER_WORKER`). Both halves of that matter. Entry count is
the right unit because decode is paid once per entry regardless of that
entry's byte length or `run_length`; weighting by cumulative bytes or by
output-tile count was tried in production and failed badly in opposite
directions (see `prepare_shards.py`'s `partition_contiguous()`, which
documents both failures in full and applies the same rule when splitting
work across workers). But equal entry counts still don't mean equal cost -
transform cost tracks tile content, and four near-identically-sized chunks
have finished tens of minutes apart - which is why chunk count exceeds
process count: that turns `ProcessPoolExecutor`'s own call queue into a
work queue, where a process that finishes early pulls the next pending
chunk instead of idling while one unlucky core grinds through a dense
coastline. Balancing gets the chunks roughly even; over-chunking absorbs
whatever imbalance is left. Each task reloads its
profiles from their own `--profile` paths rather than receiving live
instances (profiles loaded via
`load_profile()`'s `importlib.util.spec_from_file_location()` aren't
registered in `sys.modules`, so the default pickler used to hand work to a
pool worker can't reconstruct them there). `tests/test_low_zoom_regression.py`
(run on every push/PR via `.github/workflows/test.yml`) checks both of these
changes against real OpenFreeMap data at low zoom, confirming identical
output to the old per-profile, single-process behavior.

### Worker independence

Every `build-shards` matrix cell is fully independent, by construction:

- Its **only** inputs are its own `manifests/worker-NNN.bin` and the shared,
  read-only `manifests/source.json`. No worker reads another worker's
  manifest, output, or logs.
- There is **no shared mutable state anywhere in the run**: no queue, no
  claim file, no lock, no timing history, no state branch. Nothing a worker
  does is visible to any other worker.
- Its output is one `<basename>-shard-<N>.mbtiles` per profile, named by its
  own index, uploaded under its own artifact name, so two cells can never
  collide on a path.
- Its work is fixed before it starts, by `prepare-shards`. A cell computes
  the same result whenever it runs.

So execution order is irrelevant, cells may run concurrently or serially in
any interleaving, and a single failed cell can be re-run on its own without
touching the others. `fail-fast: false` is set for exactly that reason: one
cell failing is not evidence about any other, so the rest are allowed to
finish.

This is the one structural difference from
[TileDistillery](https://github.com/foxandfeature/tiledistillery), which
*does* run a claim queue with shared state on the caller's `state` branch.
The reason is the input, not a difference of opinion: TileDistillery's units
of work are Geofabrik regions — named, stable across runs, wildly uneven in
size — so it pays for a queue to get timing history and longest-first
ordering out of it. TileAlchemist slices its own shards out of the source
archive on every run, so a shard has no identity that survives to the next
run, nothing to accumulate history against, and no size skew left to
schedule around: `partition_contiguous()` has already balanced the cells
before any of them start. A queue here would add coordination, shared
state, and a failure mode, and buy nothing.

## Publishing

`.github/workflows/_pipeline.yml` is a **reusable** workflow
(`on: workflow_call`) containing only `prepare-shards` → `build-shards` →
`merge`, parameterized by `profile`, `source`, `output_basename`, and
`attribution`. `profile`/`output_basename` each take one value (e.g.
`profile: tilealchemist/profiles/land.py`) or a comma-separated list
matched 1:1 (e.g. `output_basename: land,cropped-waterways`), so
`prepare-shards` and each worker's fetch happen once per run regardless of
how many profiles are built (see "Fetching"/"Parallelism" above): its
`build-shards` step passes the full profile list to one
`tilealchemist-build-shard` invocation per worker (comma-separated
`--profile`/`--out`, matched 1:1), bundles every profile's shard file for
that worker under one artifact, and its `merge` job matrixes over
`{profile, output_basename}` pairs, each producing its own
`<output_basename>.pmtiles`, uploaded as its own workflow artifact. It
deliberately does **not** publish anywhere, so a third-party caller (see
[`docs/PROFILES.md`](PROFILES.md)) is never forced through this repo's own
credentials. This is a documented, cross-repo, public contract. Callers
reference the `<output_basename>-pmtiles` artifact directly by name
rather than through a job output, since that name is fully deterministic
regardless of profile count, whereas GitHub Actions doesn't guarantee
which matrix cell's value wins for a job-level output across multiple
`merge` matrix cells.

Publishing is two more reusable workflows, called once per profile by
`build-land-and-waterways.yml` (four jobs total: two profiles x two publish
targets); both only ever consume one named `<output_basename>-pmtiles`
artifact, regardless of how many profiles `_pipeline.yml` built it
alongside. Not equally safe to call from *outside* this repo:

- **`_publish-release.yml`** (`output_basename`, `tag`, `title`,
  `min_zoom`, `max_zoom` inputs) downloads the `<output_basename>-pmtiles`
  artifact, splits it
  into numbered parts if needed, and publishes/replaces a fixed-tag
  GitHub Release. Safe to call cross-repo: it only uses the automatic
  `secrets.GITHUB_TOKEN` and `github.repository`/`github.run_number`,
  which always reflect the *calling* repository inside a called reusable
  workflow's job (unlike `environment:`-scoped secrets specifically, which
  are documented as not flowing from caller to callee).
- **`_publish-b2.yml`** (`output_basename` input) downloads the same
  `<output_basename>-pmtiles` artifact and mirrors it to Backblaze B2
  under that name as the object key, gated behind the `b2-publish`
  GitHub Environment (required-reviewer approval before credentials are
  used). **Not callable cross-repo, and not just by convention**: a job's
  `environment:` secrets inside a reusable workflow resolve against the
  repository that owns the *workflow file*, not the caller's, so any
  repository could otherwise reach this repo's real B2 credentials just by
  calling this public file directly - a `github.repository ==
  'foxandfeature/tilealchemist'` job-level `if:` guard blocks the job from
  ever starting (before the environment gate, before any secret is
  touched) for every repository but this one. A third-party adopter should
  write its own publish job with its own `environment:`/secrets instead.

Every `publish-*` job `needs:` the job that calls `_pipeline.yml`, the one
real cross-job dependency; everything else flows through `inputs.*` or
named artifacts. A third-party repo calls
`_pipeline.yml` via
`uses: foxandfeature/tilealchemist/.github/workflows/_pipeline.yml@<ref>`,
a native cross-repo capability of reusable workflows, no GitHub Marketplace
listing required.
