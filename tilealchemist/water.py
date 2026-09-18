"""Shared water-polygon geometry math.

For profiles that subtract a tile's real surface water from something:
inverting it into land, cropping other geometry against it.

Knows nothing about layers or attributes. Deciding which polygons count as
real surface water is the schema's job (the `SURFACE_WATER` feature set).
This module is the geometry step after that, kept in one place so two
profiles can never compute it even slightly differently. docs/PROFILES.md
"Subtracting water" says why subtract_water() is an operation a profile
calls rather than an operand it fetches and cuts with itself.
"""
import shapely
from shapely.ops import unary_union

from tilealchemist.features import SURFACE_WATER
from tilealchemist.mvt import OUTPUT_GRID_SIZE

# Half a cell of the grid output coordinates snap onto, buffered out around
# the union before anything is cut with it. It closes the sub-unit gaps
# between adjoining water features that snapping would otherwise round up
# into visible slivers of land across the water. Costs 1/32 px off every
# coastline. docs/PROFILES.md "Subtracting water" has the case.
#
# Buffering can itself invalidate the union; _compute_union() repairs it.
WATER_GAP_CLOSING_BUFFER = OUTPUT_GRID_SIZE / 2


def surface_water_union(tile):
    """Closed union of `tile`'s `SURFACE_WATER` polygons, or None if it has
    none. Computed once per tile, however many profiles ask."""
    return tile.derived("water", "surface_water_union", _compute_union)


def _compute_union(tile):
    polygons = [feature.geometry.buffer(0)
                for feature in tile.features(SURFACE_WATER)]
    if not polygons:
        return None
    # make_valid() repairs the buffer, it does not reshape it. GEOS's buffer
    # can pinch a narrow hole (a skerry) off the union's shell and emit the
    # remnant as a duplicate element overlapping the body it came from,
    # leaving the union invalid with `Nested shells`. Repaired here rather
    # than at the cut: an invalid union is undefined behaviour for every
    # consumer.
    #
    # `method="structure"` is REQUIRED. The default "linework" splits the
    # overlap instead of merging it, leaving a phantom island where the
    # duplicate was — the artefact class WATER_GAP_CLOSING_BUFFER exists to
    # prevent. "structure" merges it, measurably identical to unioning the
    # parts with each other. `keep_collapsed=False` keeps the result
    # polygonal, so no caller is handed a line where it expects an area.
    #
    # Seen on openfreemap planet 20260906_080001, openmaptiles schema,
    # profile `land`, `subtract_water(tile, tile.buffered_square)`, tile
    # z9/269/151 (southern Norway), shapely 2.1.2: a duplicate 2.209-unit
    # element at 2292.535, 471.817 aborted the shard with
    # `TopologyException: unable to assign free hole to a shell` at
    # 2292.538, 471.809. The unbuffered union cut fine, which places the
    # defect here rather than at the cut.
    return shapely.make_valid(
        unary_union(polygons).buffer(WATER_GAP_CLOSING_BUFFER),
        method="structure", keep_collapsed=False)


def subtract_water(tile, geometry):
    """`geometry` minus `tile`'s real surface water, returned unchanged if
    the tile has none. Shapely in, shapely out.

    An operation rather than an operand, so every profile in a run cuts
    against identical water. `land` inverts it while `cropped-waterways`
    cuts lines against it; water computed even slightly differently in the
    two would show as a seam where a cropped river meets the land polygon it
    should butt up against.

    Default float precision suffices: the operand is valid by construction
    (see _compute_union()). `grid_size=OUTPUT_GRID_SIZE` costs ~2.7x the time
    and still does not hand `encode_tile()` geometry its snap leaves alone.
    docs/PROFILES.md "Subtracting water" has the numbers and the condition
    under which it SHOULD come back.
    """
    union = surface_water_union(tile)
    if union is None:
        return geometry
    return geometry.difference(union)
