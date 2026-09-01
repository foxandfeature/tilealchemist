# TileAlchemist

A pipeline for building global PMTiles layers from an existing
OpenStreetMap-based PMTiles source, tile by tile, driven by pluggable
**profiles**: a profile decides what each tile becomes. Fetching, sharding
(128-way by default, configurable), merging, and publishing are the
pipeline's job; a profile is just the per-tile transform plugged into it.
Profiles don't even have to live in this repository, see
[`docs/PROFILES.md`](docs/PROFILES.md).

## Profiles shipped today

| Profile | Produces | Example style |
| --- | --- | --- |
| `land` | Every tile's land polygon(s), derived by inverting the source's `water` layer. | [`examples/land.json`](examples/land.json) |
| `cropped-waterways` | The source's `waterway` lines, cropped to the portions that don't overlap real water. | [`examples/cropped-waterways.json`](examples/cropped-waterways.json) |

These are examples of what the pipeline can produce, not the whole point of
the project; more are expected over time. See
[`docs/EXAMPLE_PROFILES.md`](docs/EXAMPLE_PROFILES.md) for what each one
computes and why, or [`docs/PROFILES.md`](docs/PROFILES.md) to write your
own.

## Using the prebuilt layers

Finished PMTiles files are published on every run; you don't need to run
the pipeline yourself just to use them. Preview a profile's output directly
in Maputnik:
[`land`](https://maplibre.org/maputnik/?style=https://raw.githubusercontent.com/foxandfeature/tilealchemist/main/examples/land.json),
[`cropped-waterways`](https://maplibre.org/maputnik/?style=https://raw.githubusercontent.com/foxandfeature/tilealchemist/main/examples/cropped-waterways.json).

Each layer carries its attribution (`TileAlchemist`, linked to this repo,
`· OpenFreeMap © OpenMapTiles Data from OpenStreetMap`, per
[OpenFreeMap's attribution guidance](https://github.com/hyperknot/openfreemap#attribution))
in its own metadata, so a MapLibre style pointing at it through the
[PMTiles protocol](https://github.com/protomaps/PMTiles) picks it up
automatically, no separate TileJSON or manual attribution string needed:

```js
import { Protocol } from "pmtiles";

const protocol = new Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);

const map = new maplibregl.Map({
  style: {
    version: 8,
    sources: {
      land: {
        type: "vector",
        url: "pmtiles://https://f003.backblazeb2.com/file/tilealchemist/land.pmtiles",
      },
    },
    layers: [
      { id: "land", type: "fill", source: "land", "source-layer": "land" },
    ],
  },
  // ...
});
```

Or grab a file directly from its GitHub release, tagged `<profile>-latest`
(e.g. [`land-latest`](https://github.com/foxandfeature/tilealchemist/releases/tag/land-latest),
[`cropped-waterways-latest`](https://github.com/foxandfeature/tilealchemist/releases/tag/cropped-waterways-latest)):
large builds ship as multiple parts, the release description has a
one-line `gh release download` command that reassembles them.

## Running it

Both profiles are built together by
`.github/workflows/build-land-and-waterways.yml`, triggered manually
(`workflow_dispatch`, inputs `min_zoom`/`max_zoom`) or on a monthly
schedule. It calls
`_pipeline.yml` once with both profiles (`profile`/`output_basename` accept
a comma-separated list), sharing one `prepare-shards` walk and one fetch
per worker, so OpenFreeMap only sees each tile's bytes fetched once per
run regardless of how many profiles are built from it; each profile's
transform then runs against those same fetched bytes. The finished
`.pmtiles` files are uploaded both as workflow artifacts and as
GitHub Releases, replaced on every run rather than kept alongside older
ones.

There's nothing to install or configure to trigger a run; it's all driven
from GitHub Actions. `pyproject.toml` and the `tilealchemist/` package
below are only relevant if you're reading, changing, or extending the
pipeline itself.

## Architecture at a glance

Three small abstractions keep the pipeline generic: a **`Profile`** decides
what each tile turns into, a **`Source`** decides where the input PMTiles
archive comes from, and a **`TileSchema`** decides what its layers/attributes
are actually called.

- [`docs/PROFILES.md`](docs/PROFILES.md): the `Profile`/`TileSchema`
  contracts and how to write and distribute a profile of your own.
- [`docs/EXAMPLE_PROFILES.md`](docs/EXAMPLE_PROFILES.md): what `land` and
  `cropped-waterways` actually compute.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): `Source` resolution,
  batched fetching, configurable sharding, publishing, and the GitHub
  Actions structure.

## Repository layout

| Path | Purpose |
| --- | --- |
| `docs/PROFILES.md` | The `Profile`/`TileSchema` system and how to write your own profile. |
| `docs/EXAMPLE_PROFILES.md` | What the shipped `land`/`cropped-waterways` profiles compute. |
| `docs/ARCHITECTURE.md` | Pipeline mechanics: `Source` resolution, fetching, sharding, publishing. |
| `examples/` | One example MapLibre style per profile. |
| `.github/workflows/_pipeline.yml` | Reusable: prepares shards, builds them in parallel (`worker_count`, default 128), merges into one `.pmtiles` artifact per profile. `profile`/`output_basename` take one value or a comma-separated list, sharing one `prepare-shards` walk and one fetch per worker across however many profiles are given. Never publishes. Safe to call cross-repo. |
| `.github/workflows/_publish-release.yml` | Reusable: publishes a merged `.pmtiles` artifact as a GitHub Release. Safe to call cross-repo. |
| `.github/workflows/_publish-b2.yml` | Reusable: mirrors a merged `.pmtiles` artifact to Backblaze B2. Repo-internal only, see `docs/ARCHITECTURE.md` "Publishing". |
| `.github/workflows/build-land-and-waterways.yml` | Calls `_pipeline.yml` with both shipped profiles at once, sharing one fetch, then publishes each separately. |
| `pyproject.toml` | Packaging: dependencies and console scripts. Each built-in profile's own deps (e.g. `shapely`) live in `tilealchemist/profiles/requirements.txt` instead, the same convention a custom profile's own `.py` file uses. |
| `tilealchemist/prepare_shards.py` | Walks the source PMTiles directory once, partitions tiles into per-worker manifests. Needs a `Source`, not a `Profile`. |
| `tilealchemist/build_shard.py` | One worker: fetches its manifest's tiles once, runs one or more selected `Profile`s' transforms against those same fetched bytes, writes one mbtiles shard per profile. |
| `tilealchemist/profiles/` | The `Profile` ABC, plus the built-in `land`/`cropped-waterways` implementations. |
| `tilealchemist/sources/` | The `Source` ABC, plus `OpenFreeMapSource` and `StaticUrlSource`. |
| `tilealchemist/schemas.py` | The `TileSchema` ABC, `OpenMapTilesSchema`, and the `SCHEMAS` registry. |
| `tilealchemist/mvt.py` | Gzip+MVT decode/encode helper any profile can use. |
| `tilealchemist/water.py` | Geometry math for water-related profiles specifically. |
| `tilealchemist/manifest.py` | Binary manifest format shared by `prepare_shards.py`/`build_shard.py`. |
| `tilealchemist/ranged_fetch.py` | HTTP Range fetching against the source archive (session, retry/backoff, 206 enforcement, download progress), shared by `prepare_shards.py`/`build_shard.py`. |
| `tilealchemist/backoff.py`, `tilealchemist/throttle.py`, `tilealchemist/throttle_progress.sh` | HTTP retry backoff, throttled progress logging. |

## Related projects

- **[TileDistillery](https://github.com/foxandfeature/tiledistillery)** — a
  sibling pipeline solving the adjacent problem: it builds PMTiles layers
  from raw [Geofabrik](https://download.geofabrik.de) OSM extracts with
  [tilemaker](https://github.com/systemed/tilemaker), region by region,
  rather than reshaping tiles from an existing PMTiles archive. The two are
  architecturally independent — neither is input or output for the other —
  and they differ where their inputs force them to: TileDistillery's
  regions are named, recurring units, so it runs a dynamic claim queue
  ordered by each region's timing history, whereas TileAlchemist's shards
  are cut fresh on every run and carry no stable identity, so its workers
  are a plain independent matrix with no queue or history at all (see
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) "Parallelism").
- **[trashtracker-tiles](https://github.com/foxandfeature/trashtracker-tiles)**
  — a consumer of TileDistillery, not of this project: a worldwide
  waste-basket layer that supplies only a tilemaker config and Lua profile.

## Contributing

Bug reports and pull requests are welcome.

## License / attribution

The code in this repository (package, workflows, styles) is licensed under
the [MIT License](LICENSE).

The `.pmtiles` files themselves are a different matter: their data is
derived from OpenStreetMap via OpenFreeMap's planet archive, built with
OpenMapTiles (© OpenStreetMap contributors, ODbL), and that license carries
through however it's reshaped or repackaged downstream. See "Using the
prebuilt layers" above for the required attribution string and how each
`.pmtiles` file carries it automatically.
