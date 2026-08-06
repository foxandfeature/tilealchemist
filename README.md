# TileAlchemist

A pipeline for building global PMTiles layers from an existing
OpenStreetMap-based PMTiles source, tile by tile, driven by pluggable
**profiles**: a profile decides what each tile becomes. Fetching, 256-way
sharding, merging, and publishing are the pipeline's job; a profile is just
the per-tile transform plugged into it. Profiles don't even have to live in
this repository, see [`docs/PROFILES.md`](docs/PROFILES.md).

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
[`waterways-latest`](https://github.com/foxandfeature/tilealchemist/releases/tag/waterways-latest)):
large builds ship as multiple parts, the release description has a
one-line `gh release download` command that reassembles them.

## Running it

Each profile has its own `.github/workflows/build-<profile>.yml`
(`build-land.yml`, `build-cropped-waterways.yml`), triggered manually
(`workflow_dispatch`, input `max_zoom`) or on its own monthly schedule;
they run fully independently of each other. The finished `.pmtiles` is
uploaded both as a workflow artifact and as a GitHub Release, replaced on
every run rather than kept alongside older ones.

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
  batched fetching, 256-way sharding, publishing, and the GitHub Actions
  structure.

## Repository layout

| Path | Purpose |
| --- | --- |
| `docs/PROFILES.md` | The `Profile`/`TileSchema` system and how to write your own profile. |
| `docs/EXAMPLE_PROFILES.md` | What the shipped `land`/`cropped-waterways` profiles compute. |
| `docs/ARCHITECTURE.md` | Pipeline mechanics: `Source` resolution, fetching, sharding, publishing. |
| `examples/` | One example MapLibre style per profile. |
| `.github/workflows/_pipeline.yml` | Reusable: prepares shards, builds them in parallel, merges into a `.pmtiles` artifact. Never publishes. Safe to call cross-repo. |
| `.github/workflows/_publish-release.yml` | Reusable: publishes a `_pipeline.yml` artifact as a GitHub Release. Safe to call cross-repo. |
| `.github/workflows/_publish-b2.yml` | Reusable: mirrors a `_pipeline.yml` artifact to Backblaze B2. Repo-internal only, see `docs/ARCHITECTURE.md` "Publishing". |
| `.github/workflows/build-<profile>.yml` | Calls `_pipeline.yml` with one profile, then publishes. One per shipped profile, each independent. |
| `pyproject.toml` | Packaging: dependencies, console scripts, this repo's own `tilealchemist.profiles` entry points. |
| `tilealchemist/prepare_shards.py` | Walks the source PMTiles directory once, partitions tiles into per-worker manifests. Needs a `Source`, not a `Profile`. |
| `tilealchemist/build_shard.py` | One worker: fetches its manifest's tiles, runs the selected `Profile`'s transform, writes an mbtiles shard. |
| `tilealchemist/profiles/` | The `Profile` ABC, plus the built-in `land`/`cropped-waterways` implementations. |
| `tilealchemist/sources/` | The `Source` ABC, plus `OpenFreeMapSource` and `StaticUrlSource`. |
| `tilealchemist/schemas.py` | The `TileSchema` ABC, `OpenMapTilesSchema`, and the `SCHEMAS` registry. |
| `tilealchemist/mvt.py` | Gzip+MVT decode/encode helper any profile can use. |
| `tilealchemist/water.py` | Geometry math for water-related profiles specifically. |
| `tilealchemist/manifest.py` | Binary manifest format shared by `prepare_shards.py`/`build_shard.py`. |
| `tilealchemist/backoff.py`, `tilealchemist/throttle.py`, `tilealchemist/throttle_progress.sh` | HTTP retry backoff, throttled progress logging. |

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
