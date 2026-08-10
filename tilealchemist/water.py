"""Shared water-polygon geometry math, used by every profile that needs to
union a tile's real surface water (inverting it into land, or cropping
other geometry against it). Deliberately knows nothing about layers,
attributes, or schemas: extracting "which polygons count as real surface
water" from a decoded tile is `TileSchema.surface_water()`'s job (see
schemas.py), since that step is where schemas can genuinely differ in
structure, not just naming. This module only does the geometry step after
that: kept in one place so two profiles can never compute a union even
slightly differently, since a cropped waterway line and the land polygon
it should butt up against would otherwise risk disagreeing by a sub-pixel
amount and showing a visible seam once rendered together.
"""
from shapely.ops import unary_union

# Half the integer grid OUTPUT_GRID_SIZE snaps to. Adjoining water features
# that don't share vertices (e.g. a river polygon meeting a lake or the
# coastline) can leave a hairline gap between them, down to fractions of a
# unit. Left alone, that gap survives as real (if tiny) land, and snapping
# then rounds it up to a whole unit, turning a sub-pixel numerical crack into
# a visible sliver or "bridge" of land across the water (seen at a river
# mouth in tile 14/8637/5296). Buffering the water union out by half a grid
# cell before differencing closes any gap narrower than one output unit, so
# it's absorbed as water instead. Imperceptible everywhere else, since it
# shrinks every coastline by the same sub-pixel amount (1/32 px at any zoom,
# since extent:pixel ratio is constant across zoom levels).
WATER_GAP_CLOSING_BUFFER = 0.5

# Snap-rounding grid used both for overlay operations (grid_size= on
# difference()/intersection() etc.) and for shapely.set_precision() on
# encoded output, purely for robustness against a GEOS TopologyException
# seen on real-world water geometry (unable to assign free hole to a shell).
OUTPUT_GRID_SIZE = 1.0


def union_polygons(polygons):
    """Closed union of already-extracted surface-water polygons (see
    TileSchema.surface_water()), or None if there are none."""
    if not polygons:
        return None
    buffered = [polygon.buffer(0) for polygon in polygons]
    return unary_union(buffered).buffer(WATER_GAP_CLOSING_BUFFER)
