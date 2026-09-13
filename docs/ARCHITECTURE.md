# Architecture

How the pipeline works *around* a profile: resolving the source archive,
fetching it, sharding the work across workers, and publishing the result.
For what a profile actually computes, see
[`docs/PROFILES.md`](PROFILES.md) (the system) and
[tilealchemist-standardprofiles](https://github.com/foxandfeature/tilealchemist-standardprofiles)
(a worked example: the `land`/`cropped-waterways` layers).

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

1. `tilealchemist/prepare_shards.py` (the entry point; `shard_prep.py` for
   the run's flow, `pmtiles_index.py` for the walk itself, `partition.py`
   for the split that follows) walks the PMTiles directory tree (root +
   leaf directories) between `min_zoom` (default 0) and `max_zoom` **once,
   for the whole run**, not once per worker, yielding every tile's
   `(tile_id, offset, length, run_length)`.
   Depends only on the resolved `Source`, never touches tile content, so
   it's identical regardless of which `Profile` runs later. Both bounds
   prune the walk itself, not just its result: sibling entries in a
   directory are sorted and non-overlapping tile-ID ranges, so a child
   pointer whose whole range falls outside `[min_zoom, max_zoom]` is never
   even decoded, the same way `max_zoom` already skipped subtrees entirely
   past it before `min_zoom` existed (see `pmtiles_index.py`'s
   `walk_directory_tree()`).

   The walk itself issues no requests at all, and only the part of the
   index it will actually read comes down. `collect_entries()` fetches a
   64 KB prefix holding the 127-byte header and the root directory, works
   out from the root alone which leaf directories the zoom range needs, and
   fetches exactly that span. Leaf pointers are sorted by tile-ID in the
   root and the leaves sit in that same order in the file, so the needed
   ones are a single contiguous byte range, still two requests for the
   entire run, however deep the tree. This matters: a planet archive's leaf
   section is ~96 MB, while a z0..z5 run needs ~29 KB of it. An archive
   nested deeper than root + leaves can put a directory outside that span;
   the walk raises `LeafOutsideWindow` and `collect_entries()` answers by
   refetching the whole section, so correctness never rests on the layout
   assumption.
2. It sorts entries by *offset* (not tile-ID) and splits them into
   `--worker-count` (the reusable pipeline's `worker_count` input, default
   128) **contiguous** chunks, one per worker (`tilealchemist/partition.py`,
   written out by `tilealchemist/manifest.py`). Offset order tracks tile-ID
   order almost everywhere, but also catches what tile-ID order misses: an
   entry that dedupes against a *non-adjacent* tile with identical bytes
   (e.g. the same "all water" tile recurring across different oceans) lands
   in the same worker as the tile it's deduped against, instead of a random
   other worker re-fetching the same bytes. `partition.py`'s
   `partition_evenly()` never splits a run of same-offset entries across
   two workers, even past a worker's target size.
3. Each worker (`tilealchemist/build_shard.py` for the entry point,
   `shard_worker.py` for the run's flow, `fetch_batching.py` for splitting
   and fetching its manifest, `transform.py` for the CPU-bound transform,
   `mbtiles.py` for the shard files it writes) reads only its own manifest
   and fetches it with a single range GET spanning its first entry's offset
   to its last entry's end, the manifest already being a contiguous slice
   of offset order.

   Contiguous in *entries*, though, not necessarily in bytes: dedup (step 4)
   points an entry at whatever tile first held those bytes, so a run that
   starts at a high `--min-zoom` still has entries deduped against tiles
   below it, sitting far back in the file among data the run never reads.
   Two neighbouring entries can then be gigabytes apart and one GET across
   them would download all of it, so `plan_fetch_batches()` splits the
   manifest at every gap wider than `--max-fetch-gap` (8 MB by default) and
   the worker fetches, transforms and drops one batch at a time. Narrower
   gaps are fetched through, a few MB of unread bytes on an open, streaming
   connection being cheaper than another round trip against a cold CDN.

   When a worker is building multiple profiles in one invocation (a
   comma-separated `--profile` list, see "Publishing" below), it still does
   exactly one range GET per batch and reuses those same fetched bytes for
   every profile's transform, instead of fetching once per profile.
4. Reuses PMTiles' own deduplication: byte-identical tiles (e.g. a long run
   of open-ocean tiles) share one directory entry with a `run_length`,
   decoded and transformed once. Non-adjacent duplicates (step 2) show up
   as two separate entries at the same `(offset, length)`, landing next to
   each other once sorted, so `transform.py` memoizes on
   `(offset, length)` within a batch instead of re-decoding.
5. Accounts for *gaps*: tile-IDs with no directory entry at all (OpenFreeMap
   only stores a tile if it has something to render, so large empty
   stretches like desert or ice sheet interiors are simply absent).
   `partition.py`'s `compute_gaps()` finds and chunks these, tagged with a
   sentinel `length=0` so the worker asks each profile for `transform_gap`
   once and writes that at every one of their coordinates instead of
   fetching anything (see `shard_worker.py`'s `run_worker()` and
   `mbtiles.py`'s `write_gap_tiles()`).

### Worker logging

A worker's stderr is two kinds of line. **Major lines** always print, one
per phase transition, never more: source resolved, entries assigned,
`starting download`/`starting transform`, each chunk's `chunk N done`, and
the final written/skipped summary. **Update lines** (`update: ...`) exist
only so a step that is taking a while doesn't look stuck, and are throttled per
phase: transform progress at most once every 60s (`--report-interval`), per
transforming process and over its own chunk when the transform is pooled;
download progress at most once every 15s (`--download-report-interval`).
Neither prints at all if its step finishes before the first interval
elapses, so a fast worker's whole log is major lines only.

Download progress gets its own flag, and its own shorter default, rather
than sharing `--report-interval`, because the phase it covers is itself
shorter: a shard's whole-batch download is a single Range request of tens to
low hundreds of MB and routinely finishes well under 60s on a healthy
connection, so at the transform's interval it would print nothing at all,
while the transform right after it, with many chunks each logging a major
`chunk N done` line as they complete, looks much more alive by comparison.

## Parallelism

Splitting the work into many small per-worker jobs rather than fewer big
ones costs almost nothing (installing tilealchemist is seconds), and keeps
each job far from GitHub Actions' 6-hour per-job runtime limit, plus
smaller, failure-isolated jobs and fewer, smaller range requests. GitHub
also queues anything past ~20 concurrently *running* jobs on a public repo,
so a higher `worker_count` doesn't add parallelism at any one moment, just
keeps each job smaller: that's why the default is 128, not higher. Each
worker writes its own small mbtiles shard; a final job merges all shards
with `tile-join` into one `.pmtiles` file.

A worker building multiple profiles in one run (`_pipeline.yml` called with
a comma-separated `profile`) still does exactly one fetch, so per-worker
*network* time is unchanged. Per-worker *CPU* time doesn't simply scale
with the number of profiles either: `transform.py` decodes each unique tile
once into a single `Tile` object (see `tilealchemist/tile.py`) and hands
that same object to every profile back-to-back (entries outer, profiles
inner in `transform_batch_blob_multi()`). Anything derived from a tile (its
decoded layers, a schema feature set, `water.py`'s
`surface_water_union()`) is memoized on that object, so a second profile
reading the same tile gets the first one's result rather than repeating the
gunzip+protobuf decode or the polygon-union work. The `Tile` is dropped
when the entry is done, which bounds that sharing's memory to one tile at a
time. Two profiles that don't share any of that underlying work (a
hypothetical buildings profile alongside `land`, say) still each pay their
own cost in full: only genuinely-shared steps (decode, and a water profile
pair's own shared water union) come out cheaper.

Independently of that, the transform phase itself (decode, transform,
encode, sqlite insert, all of them CPU-bound rather than network) fans out
across a worker's own CPU cores via `--transform-workers` (default: all
available cores; `1` disables pooling and runs everything in the worker
process).
`real_entries` is split into contiguous chunks sized by **entry count**,
deliberately producing several times more chunks than there are processes
(`TRANSFORM_CHUNKS_PER_WORKER`). Both halves of that matter.

Entry count is the right unit because decode is paid once per entry
regardless of that entry's byte length or `run_length`; weighting by
cumulative bytes or by output-tile count was tried in production and failed
badly in opposite directions. Balancing on bytes let one real run hand a
worker 3.5M real entries against its peers' 500K-900K, a near-identical
download size at ~5x the decode work, which ran that worker out of memory.
Balancing on `run_length` (total output tiles) was worse: a handful of
entries with a huge `run_length` "fills" a tile-sized target almost
immediately while costing almost no decode work, so regions dense in those
pushed every real, unique-content entry (`run_length` 1, one decode each,
and just as many bytes to fetch) onto whatever workers were left, producing
a 4.3M-entry/16GB worker next to a 76K-entry/7MB one. `partition.py`'s
`partition_evenly()` applies the same rule when splitting work across
workers in the first place.

But equal entry counts still don't mean equal cost: transform cost tracks
tile content, not entry count, and a real run's four chunks of
269648/269648/269648/269645 entries finished 2m17s, then a further 9m15s,
then a further 17m9s apart (dense coastline vs. open ocean). That is why
chunk count exceeds process count: it turns `ProcessPoolExecutor`'s own
call queue into a work queue, where a process that finishes early pulls the
next pending chunk instead of idling while one unlucky core grinds through
a dense coastline. Balancing gets the chunks roughly even; over-chunking
absorbs whatever imbalance is left. `TRANSFORM_CHUNKS_PER_WORKER` is 8:
enough to keep the idle tail at roughly an eighth of a process's share,
while leaving per-chunk overhead (one profile reimport, one blob-slice
pickle) noise against multi-minute chunks.

Each task reloads its profiles from their own `--profile` paths rather than
receiving live instances: profiles loaded via `load_profile()`'s
`importlib.util.spec_from_file_location()` aren't registered in
`sys.modules`, so the default pickler used to hand work to a pool worker
can't reconstruct them there. `.github/workflows/test.yml` runs both of
these paths against real OpenFreeMap data on every push/PR, as a normal
low-zoom run rather than as a separate test.

Each chunk's output is written to its profile's mbtiles as soon as that
chunk comes back, then dropped: `run_transform()` yields each chunk's
results as it completes and `shard_worker.py` writes that chunk out
immediately, rather than collecting every chunk's output first. A worker
with millions of real entries would otherwise hold its entire shard's
transformed output in memory for the whole transform phase, which is
exactly what ran a worker out of memory in production. This way peak memory
is bounded by how many chunks are in flight at once, not by the shard's
total size.

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
  collide on a path. Every profile's file for one worker travels inside that
  one artifact, which is safe because the fixed `-shard-<N>.mbtiles` suffix
  keeps one basename's files out of another basename's per-profile glob in
  `merge`. Producing no file at all, for some or all profiles, is expected
  rather than a failure (an all-ocean slice has no waterway to crop), hence
  the upload's `if-no-files-found: ignore`.
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
of work are Geofabrik regions, which are named, stable across runs and
wildly uneven in size, so it pays for a queue to get timing history and
longest-first ordering out of it. TileAlchemist slices its own shards out of
the source archive on every run, so a shard has no identity that survives to
the next run, nothing to accumulate history against, and no size skew left
to schedule around: `partition.py`'s `partition_evenly()` has already
balanced the cells before any of them start. A queue here would add
coordination, shared state, and a failure mode, and buy nothing.

## Publishing

`.github/workflows/_pipeline.yml` is a **reusable** workflow
(`on: workflow_call`) containing only `prepare-shards` → `build-shards` →
`merge`, parameterized by `profile`, `profile_artifact`, `source`,
`output_basename`, and `attribution`. `profile`/`output_basename` each take
one value (e.g. `profile: ./land.py`) or a comma-separated list
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
`merge` matrix cells. Each `merge` cell downloads *every* profile's shard
artifacts and picks its own out by filename prefix, even on a single-profile
run where there is only one set to download. That costs CI-internal
bandwidth and nothing else: the source archive is never touched again after
`build-shards`.

### Getting the profiles in

`_pipeline.yml` never checks the calling repository out. Profiles reach it
as **an artifact the caller uploads before calling it**, named by the
required `profile_artifact` input, which `build-shards` unpacks at the
workspace root so the paths in `profile` read exactly like repo-relative ones
(`./my_profile.py`). `prepare-shards` downloads it too, but only to fail a
`profile` path that isn't in the artifact once, in seconds, rather than
identically in all `worker_count` build cells after the archive walk has
already run.

The alternative, a bare `actions/checkout` (which inside a called reusable
workflow resolves to the *caller's* repository), would work for a profile
committed to the calling repo and only for that. `build-shards` doesn't need
a repository, it needs one `.py` file; taking that file as an artifact means
it can come from anywhere the calling workflow can produce one: another
repository (this repo's own `test.yml` builds with
[tilealchemist-standardprofiles](https://github.com/foxandfeature/tilealchemist-standardprofiles)'
profiles that way, having none of its own), a generator step, a downloaded
release asset. It also replaces `worker_count` full checkouts with one
upload and `worker_count` downloads of a file measured in kilobytes.

tilealchemist itself is a different matter, and *is* checked out: every job
takes it into `.tilealchemist/` from `job.workflow_repository` at
`job.workflow_sha` (the repository and commit of the workflow file that
defines the job, i.e. this pipeline itself), so the package always matches
the pipeline running it, and a fork picks up its own copy. Those are the
`job` context's properties, not the `github` context's lookalikes, which
inside a called reusable workflow describe the caller instead;
`github.job_workflow_sha` in particular exists only as an OIDC token claim
and is empty in the `github` context
([actions/runner#2417](https://github.com/actions/runner/issues/2417)).

The one place this breaks down is GitHub Enterprise Server, where the
`job.workflow_*` properties are not available at all. An empty `ref` makes
`actions/checkout` fall back to the default branch, which would build
against the wrong tilealchemist commit without any error, so
`prepare-shards` asserts both values are non-empty before its own checkout,
once, in the run's first job, rather than three times over.

A profile's own dependencies then come from the PEP 723 block inside that
profile file, read without importing it; see
[`docs/PROFILES.md`](PROFILES.md). That is also why one file is genuinely
enough to ship through an artifact.

One more reusable workflow handles the half of publishing that is generic.
**`_publish-release.yml`** (`output_basename`, `tag`, `title`, `min_zoom`,
`max_zoom` inputs) downloads one named `<output_basename>-pmtiles` artifact,
splits it into numbered parts if needed, and publishes/replaces a fixed-tag
GitHub Release. It is safe to call cross-repo because it only uses the
automatic `secrets.GITHUB_TOKEN` and `github.repository`/`github.run_number`,
all of which reflect the *calling* repository inside a called reusable
workflow's job, so the release lands in the caller's repo, under the
caller's own token. Its own two mechanics are worth stating, because both
look like arbitrary choices in the workflow file.

### Releasing

The release **tag is fixed and replaced on every run** (`land-latest`, not
`land-2026-09`), so a link to a layer keeps working and never has to be
chased to a new version. Replacing means deleting first: `gh release delete
--cleanup-tag` removes the underlying git tag along with the release, so
tags don't accumulate either. That delete is expected to fail on a
repository's very first run, when there is no prior release to remove, and
is ignored for exactly that reason: the alternative would be a conditional
that is wrong once and pointless forever after.

A finished planet layer routinely exceeds GitHub's per-asset size limit, so
anything over 1900 MiB is **split into numbered `.partNNN` assets**. That
makes reassembly the downloader's problem, which is why the generated
release notes carry a ready-made `gh release download` line for the exact
release: one command fetches every asset at once, whether or not the file
was split, instead of the reader having to work out the part names and
`curl -O` each one. The Windows variant differs only in `copy /b` versus
`cat`.

### Why publishing stops there

Anything needing a credential of its own stays out of this repository, in
the repo that owns that credential. The B2 mirror behind
[tilealchemist-standardprofiles](https://github.com/foxandfeature/tilealchemist-standardprofiles)'
layers is a plain job in that repository's own build workflow, not a
reusable workflow here, and that is a deliberate structural choice rather
than tidiness.

The reason is that `environment:`-scoped secrets are the one part of the
`github`-context story that does *not* follow the caller: a job's
`environment:` inside a reusable workflow resolves against the repository
that owns the **workflow file**. A public reusable B2 job living here would
therefore hand *this* repository's real B2 credentials to any repository
that called it, and would need a `github.repository ==` job-level `if:`
guard to prevent that, a guard that works, but that only exists to undo a
problem created by putting the job in the wrong repository. A job in the
repository that owns the credentials needs no guard at all, only its own
environment's approval gate.

The same reasoning is why `_pipeline.yml` publishes nowhere: a caller's
`environment:`/secrets have to resolve against the caller.

Every `publish-*` job in a caller's workflow `needs:` the job that calls
`_pipeline.yml`, the one real cross-job dependency; everything else flows
through `inputs.*` or named artifacts. A third-party repo calls
`_pipeline.yml` via
`uses: foxandfeature/tilealchemist/.github/workflows/_pipeline.yml@<ref>`,
a native cross-repo capability of reusable workflows, no GitHub Marketplace
listing required.
