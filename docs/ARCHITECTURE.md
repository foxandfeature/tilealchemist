# Architecture

How the pipeline works *around* a profile: resolving the source archive,
fetching it, sharding the work across workers, and publishing the result.
For what a profile actually computes, see
[`docs/PROFILES.md`](PROFILES.md) (the system) and
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)
(a worked example: the `land`/`cropped-waterways` layers).

## Source resolution

`Source.resolve()` (`tilealchemist/sources/`) finds the PMTiles archive URL
to read from, independent of which profile is running, and says which schema
that archive's tiles are in.

Both, not just the URL. A provider publishes one schema, not a different one
per build, so which schema to decode through is the source's own property
rather than a second thing a caller has to get right alongside it: `--source
protomaps` cannot be read as OpenMapTiles by accident, because there is no
flag left with which to say so. A source declares the `TileSchema` itself,
not a name for it, so a declaration cannot be misspelled into a lookup that
fails somewhere downstream; the name it travels under appears once it has
to cross a file, in `source.json` (`manifest.py`), which is also what keeps
a worker from disagreeing with the walk about it. That closes a failure that is otherwise silent rather
than loud: both schemas here have a `water` layer, so a run pairing one
provider's archive with the other's schema doesn't crash, it produces
plausible nonsense — OpenMapTiles' reader would take a Protomaps tile's
waterway lines and label points for water polygons and filter them on a
`brunnel` attribute that isn't there.

`OpenFreeMapSource.resolve()` (the default) re-resolves on every run:
`files.txt` is scanned for the newest `areas/planet/<timestamp>_pt/`
directory that has **both** a `done` marker and a `tiles.pmtiles` file (the
very newest directory listed isn't necessarily done converting yet).

`ProtomapsSource` (`--source protomaps`) re-resolves the same way against a
different shape of listing: `build-metadata.protomaps.dev/builds.json` names
every build in the bucket, and the newest build *date* wins, not the newest
entry, because the index also keeps the last build of each older basemap
version around. It needs no equivalent of the `done` marker: the index is
generated from the bucket's object listing, where an object appears only
once its upload has finished. Re-resolving every run is not optional here
the way it nearly is for OpenFreeMap, since Protomaps keeps only the last
seven days of daily builds, so any build named in a workflow file would be
a 404 within the week.

`StaticUrlSource` (`--source static-url --source-url ...`) skips that
resolution for any other PMTiles provider that just publishes one file at a
stable location. It is the one source that cannot name its own schema, a
bare URL being nobody's provider in particular, so it is the one that takes
`--schema` (the pipeline's `schema` input) and requires it. Passing one to
any other source is refused rather than ignored: a `--schema` that
contradicts what the source publishes is a misunderstanding worth stopping
for, and one that agrees is merely redundant. It is also the way to read a copy of your own, which is
what Protomaps asks for if a *deployed* map is what would be reading it
(docs.protomaps.com/basemaps/downloads discourages hotlinking their builds,
whose URLs can move); one archive walk per build run is the download that
page describes, not the serving it warns about.

## Source attribution

What a layer credits is a statement about the data it was built from, so the
pipeline reads it off the archive that was walked rather than taking the
caller's word for it. PMTiles v3 keeps a JSON metadata document between the
root directory and the tile data (spec section 4), and both sources this
repository ships state an `attribution` in it. They do not state the same one:

    OpenFreeMap   OpenFreeMap, &copy; OpenMapTiles, &copy; OpenStreetMap contributors
    Protomaps     &copy; OpenStreetMap

(anchors in the archives, flattened here). A constant in a workflow file
cannot follow that difference. Switching `source` leaves the old string in
place and nothing in the run disagrees: the layer publishes, crediting a
provider whose bytes it never read.

`attribution.py` reads it during `prepare-shards`, for one ranged request of
about a kilobyte. The metadata lies outside the 16 KB prefix
`pmtiles_index.py` already holds — OpenFreeMap writes it just past that
boundary, Protomaps at the end of a 137 GB archive — so it is cheap rather
than free.

What the run makes of it is the caller's business. The `attribution` input is
a template in which `{source}` stands for what the archive declared:

| `attribution` | the layer credits |
| --- | --- |
| unset | the archive's own attribution, unchanged |
| `<a ...>&copy; Example</a> {source}` | the caller's name, then the archive's |
| `&copy; Example` | that, instead of the archive's |

This belongs to whoever runs the pipeline rather than to the profile. A
profile can be a file downloaded from anywhere, and editing it to add a name
is not something a caller should have to do; the credit is a property of the
run, not of the transform. It is also why one answer serves every profile in a
run: `prepare-shards` resolves the source, walks it and reads its metadata
without importing caller code at all.

The substitution happens in `compose_attribution()`, not in the workflow's
shell, and that is load-bearing: bash's `${v//p/r}` expands an unescaped `&`
in the replacement to the matched text, and both sources' attributions are
full of `&copy;`, so composing there would corrupt exactly the strings this
exists to carry.

An empty attribution is refused rather than published. An archive declaring
none with no template to stand in for it, a template whose `{source}` has
nothing to fill it with, and a template that composes to nothing all fail the
run inside `prepare-shards`, before a worker is dispatched. `merge` asserts
the value once more before `tile-join` stamps it, the value having crossed a
job output in between.

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
   16 KB prefix holding the 127-byte header and the root directory, works
   out from the root alone which leaf directories the zoom range needs, and
   fetches exactly that span: two requests for the entire run. 16 KB is not
   a guess -- PMTiles v3 (spec section 4) requires header plus root to fit
   in the first 16,384 bytes precisely so one blind fetch can get them, so
   the root never needs a request of its own. The pruning matters: a planet
   archive's leaf section is ~96 MB, while a z0..z5 run needs ~29 KB of it.

   The span is the first to the last leaf the root points into, which relies
   on two things the same spec section asks of writers: leaf order SHOULD
   ascend by tile-ID, and more than one level of leaf directories is
   discouraged. Neither is a MUST, so both are checked rather than trusted --
   `LeafWindow.node_bytes()` raises on any pointer outside the span instead
   of slicing whatever bytes sit there. Reading such an archive would mean
   pulling the whole leaf section, and no published archive is built that
   way (verified against OpenFreeMap and Protomaps planet builds, whose root
   pointers tile their leaf section back-to-back with no gaps), so it errors
   out rather than carrying a fallback path that never runs.

   Nor does it rest on where in the file that section *is*: the header says,
   and writers disagree. OpenFreeMap's archives run
   Header/Root/Metadata/LeafDirs/TileData, Protomaps' daily builds put the
   leaf directories and metadata *after* 137 GB of tile data, and both read
   identically here because every offset comes out of the header rather than
   out of an assumed order. A run reads 29 KB of OpenFreeMap's index and
   132 KB of a Protomaps build's for z0..z4.
2. It sorts entries by *offset* (not tile-ID) and splits them into
   `--worker-count` (the reusable pipeline's `worker_count` input, default
   128) **contiguous** chunks, one per worker (`tilealchemist/partition.py`,
   written out by `tilealchemist/manifest.py`). Offset order tracks tile-ID
   order almost everywhere, but also catches what tile-ID order misses: an
   entry that dedupes against a *non-adjacent* tile with identical bytes
   (e.g. the same "all water" tile recurring across different oceans) lands
   in the same worker as the tile it's deduped against, instead of a random
   other worker re-fetching the same bytes. `partition.py`'s
   `partition_by_cost()` keeps a run of same-offset entries whole across
   worker boundaries, past a worker's target size, but only up to one whole
   share of the run's cost (see "Parallelism" for what that weighs). That
   cap is not a detail: a Protomaps planet build dedupes its open ocean into
   same-offset runs of hundreds of thousands of entries (315K and 306K at
   z0..z11 alone, against a 128-worker share of 16K), and an unbounded rule
   drops every one of them on a single worker however high `worker_count`
   goes, while starving the workers after it. Splitting such a run costs
   only what keeping it whole was buying — one tile's bytes re-fetched per
   extra worker — so the cap is the cheap side of that trade.
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
`real_entries` is split into contiguous chunks by `partition.py`'s
`partition_by_cost()`, the same function that splits work across workers,
deliberately producing several times more chunks than there are processes
(`TRANSFORM_CHUNKS_PER_WORKER`). Both halves of that matter.

### What a record costs

`cost.py` answers that, and `partition_by_cost()` splits on cumulative cost
rather than on a record count. A record's cost has three axes, and no
single one of them is the unit:

- **Decode work**, which dominates. It is paid once per *distinct* entry —
  a record repeating the previous one's `(offset, length)` is decoded zero
  times, matching what `transform_batch_blob_multi()` actually does — and
  it grows *faster* than that entry's byte length, because a bigger tile is
  also a denser one: more features to decode, more geometry for a profile
  to clip and union. `DENSITY_EXPONENT` is 1.5, fitted against a planet
  run's 128 worker durations, where it lifts the model's R² from 0.67
  (linear in bytes) to 0.77.
- **Entry count**, which bounds decode calls and, with them, peak memory.
- **`run_length`**, one sqlite insert per output tile per profile.

Each axis is normalized to its own total across the run, then mixed by
`AXIS_SHARES` (0.25 / 0.60 / 0.15). Normalizing first is what makes the mix
safe: a worker can exceed its fair share of any one axis only by that
axis's reciprocal share, so none can run away. That matters because
weighting by a single axis was tried in production and failed badly in
opposite directions. Balancing on bytes alone let one real run hand a
worker 3.5M real entries against its peers' 500K-900K, a near-identical
download size at ~5x the decode work, which ran that worker out of memory.
Balancing on `run_length` alone was worse: a handful of entries with a huge
`run_length` "fills" a tile-sized target almost immediately while costing
almost no decode work, so regions dense in those pushed every real,
unique-content entry (`run_length` 1, one decode each, and just as many
bytes to fetch) onto whatever workers were left, producing a
4.3M-entry/16GB worker next to a 76K-entry/7MB one.

Counting records, the unit used before this, fails the other way: it is
*exactly* even and says nothing about cost. In a 128-worker planet run it
gave every worker 458K entries and between 32MB and 3,494MB of tile data,
and those workers ran between 26s and 57m42s. Across all 128, job duration
correlated with entry count at 0.04 and with byte volume at 0.82. Replaying
that run's manifests through the cost weight puts the longest worker at a
predicted 10m24s against 49m06s, holds the largest shard's entry count to
1.46M -- well under the 3.5M that died -- and *lowers* peak blob memory
from 3.49GB to 0.98GB.

Since only ~20 jobs run concurrently anyway, balancing does not move the
floor: 16 core-hours over 20 lanes is ~48 minutes either way. What it
removes is the tail. That run spent its last 19 minutes at a concurrency of
**one**, waiting on a single worker.

### Why more chunks than processes

Cost-balanced chunks still are not equal-cost chunks: the weight is an
estimate, and tile content varies inside a chunk. A real run's four chunks
of 269648/269648/269648/269645 entries finished 2m17s, then a further
9m15s, then a further 17m9s apart (dense coastline vs. open ocean). That is
why chunk count exceeds process count: it turns `ProcessPoolExecutor`'s own
call queue into a work queue, where a process that finishes early pulls the
next pending chunk instead of idling while one unlucky core grinds through
a dense coastline. Balancing gets the chunks roughly even; over-chunking
absorbs whatever imbalance is left. `TRANSFORM_CHUNKS_PER_WORKER` is 8:
enough to keep the idle tail at roughly an eighth of a process's share,
while leaving per-chunk overhead (one profile reimport, one blob-slice
pickle) noise against multi-minute chunks.

Both together are what close the tail. The same planet run's worst worker
split 452687 entries into 33 entry-count chunks of 0MB to 614MB, and spent
its final 11 minutes running that one 614MB chunk on one of four cores.
Weighted, the same manifest yields 32 chunks of 5MB to 107MB.

Each task reloads its profiles from their own `--profile` paths rather than
receiving live instances: profiles loaded via `load_profile()`'s
`importlib.util.spec_from_file_location()` aren't registered in
`sys.modules`, so the default pickler used to hand work to a pool worker
can't reconstruct them there. `.github/workflows/test.yml` runs both of
these paths against real OpenFreeMap data on every push/PR, as a normal
low-zoom run rather than as a separate test, and runs a second one against
a real Protomaps build alongside it (see that file for why the two calls
aren't symmetric).

Each chunk's output is written to its profile's mbtiles as soon as that
chunk comes back, then dropped: `run_transform()` yields each chunk's
results as it completes and `shard_worker.py` writes that chunk out
immediately, rather than collecting every chunk's output first. A worker
with millions of real entries would otherwise hold its entire shard's
transformed output in memory for the whole transform phase, which is
exactly what ran a worker out of memory in production. This way peak memory
is bounded by how many chunks are in flight at once, not by the shard's
total size.

Yielding per chunk only bounds it if nothing else is still holding the
chunks already yielded, and on the pooled path the `Future` each chunk came
back on is such a holder: a `Future` keeps its result for as long as the
`Future` is alive. `_pooled_chunk_results()` therefore drops each one from
its `pending` map as it yields that chunk, rather than keeping the whole map
until the pool shuts down. Keeping the whole map pinned every chunk's
output for the whole phase, putting the bound straight back where it was
before chunking — a planet run's two largest shards died to exactly that,
on runners that report it as `The runner has received a shutdown signal`.

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
to schedule around: `partition.py`'s `partition_by_cost()` has already
balanced the cells before any of them start. A queue here would add
coordination, shared state, and a failure mode, and buy nothing.

## Publishing

`.github/workflows/_pipeline.yml` is a **reusable** workflow
(`on: workflow_call`) containing only `prepare-shards` → `build-shards` →
`merge`, parameterized by `profile`, `profile_artifact`, `source`, and
`output_basename` (plus `schema`, which only a `static-url` source needs, and
the optional `attribution` template; see "Source resolution" and "Source
attribution"). `profile`/`output_basename` each take
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

One more input exists for one situation: `artifact_namespace`. The artifacts
the pipeline passes between its own jobs (`shard-manifests`, `shards-<n>`)
are named per pipeline, not per call, which two calls in the *same* workflow
run would collide over — GitHub rejects a second upload of a name already
taken in a run. A caller doing that (this repo's `test.yml`, running the
same profiles against two different sources) names one of them, and its
artifacts become `<namespace>-shard-manifests` and `<namespace>-shards-<n>`.
Callers that call the pipeline once, which is nearly all of them, never set
it.

A prefix rather than a suffix, because the `merge` job globs for the shards
it merges and a prefix is what makes those globs disjoint for free. An
unnamespaced call's `shards-*` cannot reach into `protomaps-shards-0`, so
namespacing the *second* call is enough and the first is left alone; with a
suffix, `shards-*-protomaps` would still have matched a hypothetical third
call's `shards-0-x-protomaps`, and every call would have had to be named to
be safe. It also groups an Actions run's artifact list by call rather than
by artifact kind, which is the more useful order at 128 workers.

The published `<output_basename>-pmtiles` artifacts take no namespace: two
calls in one run need distinct output basenames anyway, or their outputs
would collide by that name instead.

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
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)'
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
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)'
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
`uses: tilelab/tilealchemist/.github/workflows/_pipeline.yml@<ref>`,
a native cross-repo capability of reusable workflows, no GitHub Marketplace
listing required.

## Module invariants

Facts the code depends on that the code itself cannot state. They lived in
comments before [`COMMENT_STYLE.md`](COMMENT_STYLE.md) moved them here; each
is a constraint an edit could break silently, so change the code and this
section together.

### Phase maps

`prepare-shards`, driven by `shard_prep.run_prepare()`:

    run_prepare()
      resolve_source()                sources/: which archive to read
      make_session()                  ranged_fetch.py: shared by every fetch
      collect_entries()               pmtiles_index.py: header + index in 2 requests
      fetch_declared_attribution()    attribution.py: what the archive credits
      compose_attribution()           attribution.py: what this layer credits
      compute_gaps()                  partition.py: tile_ids no entry covers
      partition_into_worker_blocks()  partition.py: each worker's share
      write_worker_manifests()        manifest.py: worker-NNN.bin
      write_source_metadata()         manifest.py: source.json, shared

`build-shard`, driven by `shard_worker.run_worker()`:

    run_worker()
      read_source_metadata()     manifest.py: source.json from prepare-shards
      read_manifest()            manifest.py: this worker's entries
      split_manifest_entries()   -> real entries / gap entries
      init_mbtiles()             mbtiles.py: one connection per profile
      real entries (_process_real_entries), one batch at a time:
        plan_fetch_batches()       fetch_batching.py: one range GET per batch
        fetch_batch_blob()         fetch_batching.py: that batch's bytes
        run_transform()            transform.py: a chunk of tiles at a time
        write_output_tiles()       mbtiles.py: that chunk, then drop it
      gap entries (_process_gap_entries):
        Profile.transform_gap()    one blob for every gap tile in the run
        write_gap_tiles()          mbtiles.py: nothing to fetch
      close_connections()

### Zoom levels (`zoom.py`)

A zoom is a `ZoomLevel` member everywhere the pipeline passes one around, so
a level outside the set raises `ValueError` where it enters rather than
walking to nothing. `IntEnum`, because a zoom level *is* a number wherever
the pipeline computes with it — `zxy_to_tileid(max_zoom + 1, ...)`, the
`min_zoom <= zoom <= max_zoom` filter, the `2 ** zoom` row flip.

`MAX_SUPPORTED_ZOOM = 30` is a hard ceiling, not a preference:
`zxy_to_tileid()` raises `OverflowError` above z31 because `tile_id` stops
fitting a 64-bit int, and `tile_id_bounds()` always asks it for
`max_zoom + 1`. The members are generated through the functional API with
`module=`/`qualname=` set, which is what makes them picklable — `ChunkJob`
carries one into a transform pool worker.

### The manifest format (`manifest.py`)

One file per worker, a flat sequence of fixed-size records, no framing: file
size / `RECORD.size` gives the count.

    tile_id: uint64, offset: uint64, length: uint32, run_length: uint32

These mirror a PMTiles directory entry. `source.json` carries what is true
for the whole run, and its JSON key strings stay confined to
`as_json()`/`from_json()`: every other reader names a field, so a renamed or
missing key is a mistake at the two ends of the file format rather than a
`KeyError` wherever a worker happens to look something up.

### Tiles carry no coordinate (`tile.py`)

Everything on a `Tile` is tile-local, hence identical for every z/x/y that
dedupes to the same bytes. Three things rest on that invariant: one object
standing for a whole `run_length` run; one standing for two entries pointing
at the same `(offset, length)`, which offset-ordered batching makes adjacent;
and one `Tile.empty()` serving a hundred-thousand-tile gap region.

`extent` reads the first layer's. MVT allows one extent per layer, but a tile
in practice encodes every layer at the same one, and a tile with no layers
falls back to the schema's `default_extent`.

### Encoding (`mvt.py`, `mbtiles.py`)

`gzip.compress(..., mtime=0)` is required, not tidiness. Without it gzip
embeds the current time, so byte-identical tile content — every gap tile, in
particular — compresses differently across worker processes and defeats
PMTiles' content-hash dedup in the final merge.

mbtiles numbers rows TMS-style while a PMTiles tile ID decodes to XYZ, so
every write flips the row (`(2 ** zoom - 1) - tile_row`).
`write_gap_tiles()`'s `tms_rows()` must stay a generator: a worker holding a
million-tile gap (an ocean, an ice sheet interior) would otherwise
materialize them all as one list before handing them to sqlite3.

### The directory walk (`pmtiles_index.py`)

`leaf_window_for()` must prune by exactly the rule `walk_directory_tree()`
descends by. The two are kept in step by hand, because the walk pays that
rule per entry over a planet's worth of directories while the window pays it
over the root's few thousand. Leaf directories sit in the file in
root-pointer order, so the ones the walk reaches run from the first match to
the last; an archive ordering them otherwise trips `LeafWindow.node_bytes()`'s
bounds check, which is all that stands between an unexpected layout and a
silently short slice decoding into plausible-looking garbage.

`tile_id_bounds()` is derived in one place because the walk prunes against
those bounds and `compute_gaps()` fills the untouched stretches between
entries — the two must agree exactly.

`WalkProgress`'s percentage divides by the leaf window's length, which is an
upper bound: tile_id pruning lets the walk finish without decoding the whole
window, so the percentage can stop short of 100%.

### Retries (`ranged_fetch.py`)

Retries cover the transient ways a CDN fails under a cold-cache stampede,
many concurrent workers hitting a freshly-published archive at once. Three
shapes, none of them permanent:

- a 200 instead of a 206 — the server ignored the `Range` header, and reading
  the response in full would be tens of GB;
- a 429/5xx — rate-limiting, or buckling under the burst;
- the connection dropping mid-stream, seen as an `IncompleteRead` well past
  the halfway point of a large batch. There is no response left to read a
  `Retry-After` from, so the backoff runs on jitter alone.

`on_chunk(bytes_so_far)` receives the running total for the *current*
attempt, not a delta, so a retry that restarts the transfer rewinds the
progress line instead of counting the re-sent bytes twice.
`_warn_retry()` emits a `::warning` workflow command as the retry happens, so
a stampede surfaces in the Actions UI and not only in the job log.

### Batching and chunking (`fetch_batching.py`, `transform.py`)

Entries arrive sorted by offset, but sorted is not adjacent: dedup points an
entry at whatever tile first held its bytes, so two neighbours can sit
gigabytes apart with data this run never reads in between. One GET across
such a hole would download all of it, so the manifest is split at every hole
wider than `--max-fetch-gap`. Two entries at the same offset are zero apart
and must not be split. The 8 MB default is where one request still beats two:
a few MB of unread bytes on an open, streaming connection cost less than
another round trip against a cold CDN.

`_chunk_entries()` must keep chunks contiguous and in order.
`_blob_slice_for_chunk()` slices one byte range per chunk, and
`transform_batch_blob_multi()`'s dedup compares only against the previous
entry, so a duplicate pair split across a chunk boundary misses that one
dedup — harmlessly, but only because the chunks are contiguous.

`_transform_chunk()` rebuilds two things that cannot cross a process
boundary: the profiles, which the pickler cannot reconstruct in a worker at
all, reimported once per chunk rather than per tile; and its
`TransformProgress`, which holds a `threading.Lock`. Only that object is
unpicklable, not the reporting — the interval is a plain float, so a worker
throttles its own lines over its own chunk while the parent's "chunk N done"
lines carry the whole-shard view. The schema crosses as its `SchemaName`, and
the child looks the same singleton up out of `SCHEMAS`.

In `_pooled_chunk_results()`, `pending.pop(future)` must pop rather than
index. A `Future` keeps the result it was handed for as long as the `Future`
itself is alive, so holding every entry until the pool shuts down would pin
every chunk's output for the whole transform phase — exactly the memory
`run_transform()` yields per chunk to avoid.

### Profiles and gaps

`load_profile()` deliberately does not register the module in `sys.modules`.
Transform pool workers reload profiles by path, which works under both fork
and spawn; registering would only help under fork.

`compute_gaps()` tags a gap record `length=0`, the sentinel
`split_manifest_entries()` tells a gap by, there being nothing to fetch.
`GAP_CHUNK_SIZE` caps one such record so that a single huge unbroken gap — a
whole ice sheet's interior — cannot land entirely on one worker.
