# Profiles

The `Profile` system: the contract every profile implements, the shared
helpers a profile can (but doesn't have to) use, `TileSchema`, and how to
write and distribute a profile of your own. For what today's two shipped
profiles (`land`, `cropped-waterways`) actually compute, see
[`docs/EXAMPLE_PROFILES.md`](EXAMPLE_PROFILES.md) instead; they're examples
of what this system produces, not a description of the system itself. For
the pipeline mechanics around a profile (fetching, sharding, publishing),
see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

## The `Profile` contract

`tilealchemist/profiles/base.py` defines the whole contract as an ABC:

```python
class Profile(ABC):
    name: str                # CLI value, e.g. "land", "cropped-waterways"
    output_layer_name: str   # output MVT layer name
    mbtiles_name: str        # mbtiles metadata "name" value
    compatible_schemas = None  # frozenset of schemas.SCHEMAS keys this profile
                                # requires, or None if it works with any schema

    def vector_layers_json(self): ...
    def transform_tile_bytes(self, data): ...
    def transform_layer(self, decoded_tile): ...
    def gap_tile_bytes(self, zoom, tile_column, tile_row): ...
    def _encode_tile(self, features, extent): ...
```

- **`vector_layers_json()`** returns the `vector_layers` array embedded in
  the output mbtiles metadata's `json` field, e.g.
  `[{"id": "land", "fields": {}}]`.
- **`transform_layer(decoded_tile)`** is the actual per-tile work a profile
  implements: the decoded source tile (`mvt.decode_tile()`'s
  `{layer_name: {...}}` dict) in, `(features, extent)` for this profile's
  own output layer out, or `None` to skip (write nothing for this tile).
  `features` is a list of `{"geometry": ..., "properties": dict}`; which
  geometry library produces that `"geometry"` object (shapely or anything
  else `mapbox_vector_tile.encode()` accepts) is entirely the profile's own
  choice, not this method's concern. Not named after any specific operation
  like "invert" or "crop": a profile can do whatever it wants here.
- **`transform_tile_bytes(data)`** has a concrete default now: decode via
  `mvt.decode_tile()`, call `transform_layer()`, encode whatever comes back
  via `_encode_tile()`. A profile only implements `transform_layer()` in the
  common case; `transform_tile_bytes()` stays overridable directly for a
  profile with unusual needs (non-MVT output, full control over encoding).
- **`gap_tile_bytes(zoom, tile_column, tile_row)`** is called once per *gap*
  tile: a `tile_id` entirely absent from the source archive (see
  [`docs/ARCHITECTURE.md`](ARCHITECTURE.md#fetching-directory-driven-not-one-request-per-tile)
  "Fetching" step 5), so there's no source bytes to pass in. Returns bytes
  to write, or `None` to skip. Getting the tile's own coordinates means a
  profile *can* vary its answer by location or zoom; one whose content
  doesn't vary should memoize internally rather than recomputing per call,
  since this can be called hundreds of thousands of times per worker for a
  single large gap (e.g. an ice sheet interior). Left abstract (unlike
  `transform_tile_bytes()`) because that caching strategy is profile-owned;
  an implementation that needs to produce bytes should call
  `self._encode_tile(features, extent)` rather than the whole codec itself.
- **`_encode_tile(features, extent)`** wraps `mvt.encode_tile()` with this
  profile's own `output_layer_name` already filled in — the one place a
  profile touches the MVT codec directly, for cases like `gap_tile_bytes()`
  overrides that want to cache final encoded bytes rather than re-encoding
  every call.

`build_shard.py` (the generic worker script) only ever calls
`vector_layers_json()`, `transform_tile_bytes()`, and `gap_tile_bytes()`,
nothing profile-specific.

## Shared helpers a profile can use

- **`tilealchemist/mvt.py`** (`decode_tile()`/`encode_tile()`) wraps the
  gzip+MVT codec every profile ends up needing, since both the source
  archive's tiles and this pipeline's own output rows are gzipped MVT. No
  longer something a profile calls directly at all in the common case:
  `Profile`'s own `transform_tile_bytes()`/`_encode_tile()` (above) handle
  it generically, since it's always MVT regardless of which profile is
  running.
- **`tilealchemist/water.py`** (`union_polygons()`) is geometry math for
  profiles that need a closed union of surface-water polygons (already
  extracted by `schema.surface_water()`, see `TileSchema` below). Scoped to
  water-related profiles specifically; a profile working on buildings,
  POIs, or anything else has no reason to touch it. Not part of the
  `Profile` contract either, and shapely-specific by choice of the profiles
  that use it, not because `TileSchema` requires shapely (it doesn't).

## `TileSchema`

`tilealchemist/schemas.py`'s `TileSchema` is an ABC, the same pattern as
`Source` and `Profile` (see `sources/base.py`/`profiles/base.py`): a schema
implements *behavior* a profile calls, not config a profile reads and
interprets itself.

```python
class TileSchema(ABC):
    default_buffer_pixels: int
    tile_size_pixels: int = 256

    @abstractmethod
    def surface_water(self, decoded_tile):
        """Real, non-tunnel surface water polygons in this tile, as a
        SurfaceWater(extent, polygons), or None if there's no water data
        at all for this schema."""

    @abstractmethod
    def waterway_lines(self, decoded_tile):
        """This tile's waterway line features, geometry left as the raw
        MVT-decoded dict (not parsed into any geometry library) and
        properties preserved as-is, as a WaterwayFeatures(extent, features),
        or None if there's no waterway data at all for this schema."""

    @abstractmethod
    def waterway_fields(self):
        """Field name -> MVT field type for this schema's waterway line
        features, for a profile's `vector_layers_json()`."""


class OpenMapTilesSchema(TileSchema):
    default_buffer_pixels = 4

    def surface_water(self, decoded_tile):
        water = decoded_tile.get("water")
        if not water:
            return None
        polygons = [f["geometry"] for f in water["features"]
                    if f["properties"].get("brunnel") != "tunnel"]
        return SurfaceWater(extent=water["extent"], polygons=polygons)

    def waterway_lines(self, decoded_tile):
        waterway = decoded_tile.get("waterway")
        if not waterway:
            return None
        features = [{"geometry": f["geometry"], "properties": f["properties"]}
                    for f in waterway["features"]]
        return WaterwayFeatures(extent=waterway["extent"], features=features)

    def waterway_fields(self):
        # Per the OpenMapTiles schema docs (openmaptiles.org/schema/#waterway).
        return {"class": "String", "name": "String", "brunnel": "String", "intermittent": "Boolean"}


OPENMAPTILES = OpenMapTilesSchema()
```

Why behavior instead of a plain layer-name lookup table: schemas can differ
*structurally*, not just by naming, in ways a name-to-name dict can't
express. Concrete example (checked against Shortbread's real
[1.0 schema docs](https://shortbread-tiles.org/schema/1.0/), not assumed):
OpenMapTiles keeps all water in one `water` polygon layer with a single
string tunnel/bridge attribute; Shortbread splits water across two layers
(`ocean`, `water_polygons`) and uses two boolean fields instead, with no
tunnel concept on polygons at all. A `TileSchema` subclass for a schema
shaped like that overrides `surface_water()`/`waterway_lines()` with its
own logic; the existing `land`/`cropped-waterways` profiles would then work
against it unmodified, since neither reads a layer or attribute name off a
schema directly, only these two methods.

`default_buffer_pixels`/`tile_size_pixels` stay plain fields, not methods:
they describe the *tile format itself* (how much edge-buffer geometry its
tiles carry, and what pixel size that's relative to), the same for any
profile reasoning about tile edges, regardless of what layer it's looking
at.

`SurfaceWater.polygons`/`WaterwayFeatures.features[].geometry` are raw
MVT-decoded geometry dicts (exactly what `mapbox_vector_tile.decode()`
produces per-feature, e.g. `{"type": "Polygon", "coordinates": [...]}`), not
pre-parsed into any geometry library. `TileSchema`'s job is schema-structure
knowledge (which layer, which attribute means "exclude this"); parsing that
geometry into shapely, or anything else, is each profile's own choice to
make with whichever geometry library it wants — both `land` and
`cropped-waterways` happen to choose shapely (`shapely.geometry.shape(...)`)
right where they consume `surface.polygons`/`waterway.features[...]`, but
nothing in `TileSchema` requires that.

Only one schema (`openmaptiles`) exists today, but the registry it's
selected from (`--schema`, defaulting to `openmaptiles`, in
`tilealchemist/schemas.py`'s `SCHEMAS` dict) is already in place for a
second one to be added as its own `TileSchema` subclass plus one entry.

## Schema compatibility

Every `Profile` declares `compatible_schemas`: either `None` (works with any
`TileSchema`, because it only calls the universal abstract API described
above) or a `frozenset` of `schemas.SCHEMAS` keys (restricted to exactly
those schemas, because it relies on schema-specific behavior beyond that
universal contract). Both shipped profiles, `land` and `cropped-waterways`,
declare `compatible_schemas = None`: neither reads anything off a schema
except `surface_water()`/`waterway_lines()`/`waterway_fields()`/
`default_buffer_pixels`/`tile_size_pixels`.

`build_shard.py` validates `--profile`/`--schema` compatibility in
`parse_args()`, before any fetching or transforming starts: if the chosen
profile's `compatible_schemas` doesn't include the chosen `--schema`, it
fails the same way an invalid `--profile`/`--schema` value already does
(`argparse`'s `parser.error()` — usage + message to stderr, exit status 2),
rather than blindly combining an incompatible pairing and failing later
mid-run.

## Writing and distributing your own profile

A profile doesn't have to live in this repository. `tilealchemist` is a
regular pip-installable package (`pip install tilealchemist`, or
`pip install -e .` from a checkout), and its own `land`/`cropped-waterways`
profiles are discovered through the same mechanism any external package
would use: a `tilealchemist.profiles` entry point.

```toml
# your own package's pyproject.toml
[project]
dependencies = ["tilealchemist"]

[project.entry-points."tilealchemist.profiles"]
my-profile = "my_package.my_profile:MyProfile"
```

`MyProfile` implements the `Profile` ABC from `tilealchemist.profiles.base`,
including `compatible_schemas` (see "Schema compatibility" above) — omit it
and it defaults to `None`, i.e. universal, for a profile written before this
attribute existed. Once your package is installed alongside `tilealchemist`,
`tilealchemist-build-shard --profile my-profile` finds and runs it with no
changes to this repository: `tilealchemist/profiles/__init__.py`'s
`PROFILES` dict is built from
`importlib.metadata.entry_points(group="tilealchemist.profiles")`, not
hardcoded, and this repo's own two profiles are registered through that
exact same mechanism in this repo's own `pyproject.toml`, with no
"built-in vs. external" special-casing anywhere.

To run it on GitHub Actions without forking this repo, call this repo's
reusable pipeline directly from your own workflow.
`.github/workflows/_pipeline.yml`'s `build-shards` job takes an optional
`profile_package` input (a pip spec: a PyPI name, or `git+https://...`) and
`pip install`s it before resolving `--profile`. This is a native
reusable-workflow capability
(`uses: owner/repo/.github/workflows/file.yml@ref` works cross-repo for any
public or shared-private repository), no GitHub Marketplace publishing
needed (see "Is this a GitHub Marketplace Action?" below).
`.github/workflows/_publish-release.yml` is reusable the same way, if a
plain GitHub Release is all the publishing you need:

```yaml
jobs:
  build:
    uses: foxandfeature/tilealchemist/.github/workflows/_pipeline.yml@main
    with:
      profile: my-profile
      profile_package: my-package # your package on PyPI, or git+https://...
      output_basename: my-profile
      attribution: "..."
  publish:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with: { name: ${{ needs.build.outputs.pmtiles_artifact }} }
      - run: ./publish-wherever-you-want.sh
```

`_pipeline.yml` only builds and hands you the merged `.pmtiles` as an
artifact; publishing it anywhere (a GitHub Release, B2, elsewhere) is left
to your own workflow, with your own credentials. Do **not** call this
repo's `.github/workflows/_publish-b2.yml`: unlike `_pipeline.yml` and
`_publish-release.yml`, it's explicitly not meant to be called cross-repo,
see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) "Publishing" for why.

(`profile`/`output_basename` also accept a comma-separated list matched
1:1, e.g. `profile: land,cropped-waterways` / `output_basename: land,waterways`,
so several profiles can share one `prepare-shards` walk and one fetch per
worker in a single run instead of each needing its own `_pipeline.yml`
call - see this repo's own `build-land-and-waterways.yml`. A single value
behaves identically to calling it with just one profile, as shown above.)

`tilealchemist/sources/__init__.py`'s `SOURCES` and `tilealchemist/schemas.py`'s
`SCHEMAS` stay plain hardcoded dicts, not entry points, unlike `PROFILES`:
a new source or schema is a small, self-contained addition this repo's own
maintainers are expected to add directly, not the point of external
extensibility here.

### Is this a GitHub Marketplace Action?

Not in the formal "listed in the Marketplace" sense, deliberately.
GitHub Marketplace listings are for composite/Docker/JavaScript *actions*
(an `action.yml` at a repo root, one job's worth of steps), not for
reusable *workflows*. This pipeline's `strategy: matrix` 256-way fan-out is
a job-level feature; a composite action would flatten it back into one job
running everything sequentially, defeating the entire point of sharding.
Reusable workflows solve the actual goal (call a shared pipeline from
another repo with a few lines, no forking) without needing Marketplace at
all: `uses: owner/repo/.github/workflows/file.yml@ref` works cross-repo
natively for any public (or shared-private) repository. A thin
Marketplace-listed composite-action wrapper could be added later purely for
discoverability, but it would just point at the real reusable workflow, not
replace it.
