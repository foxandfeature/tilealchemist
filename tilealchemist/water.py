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
from shapely.geometry import box, shape
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


_cached_tile = None
_cached_union = None


def surface_water_union(decoded_tile, schema):
    """union_polygons() of decoded_tile's surface water, single-slot memoized
    by decoded_tile identity (see mvt.decode_tile()'s cache — repeated calls
    for the same tile get the same decoded_tile object back). Lets two
    profiles reading the same tile's water back-to-back (land +
    cropped-waterways) compute the union once instead of once each."""
    global _cached_tile, _cached_union
    if decoded_tile is not _cached_tile:
        surface = schema.surface_water(decoded_tile)
        polygons = [shape(g) for g in surface.polygons] if surface is not None else []
        _cached_union = union_polygons(polygons)
        _cached_tile = decoded_tile
    return _cached_union


def buffered_square(extent, schema):
    """Tile square buffered by schema's own edge-buffer amount (pixels ->
    units), shared by any profile needing "the tile square", same rationale
    as union_polygons()'s placement here."""
    buffer = extent * schema.default_buffer_pixels / schema.tile_size_pixels
    return box(-buffer, -buffer, extent + buffer, extent + buffer)


_cached_land_tile = None
_cached_land_extent = None
_cached_land = None


def land_polygon(decoded_tile, schema, extent):
    """Buffered tile square with surface_water_union() subtracted — the
    "invert water into land" computation, single-slot memoized the same way
    as surface_water_union() (decoded_tile identity + extent, since a caller
    could in principle ask for a different extent against the same tile)."""
    global _cached_land_tile, _cached_land_extent, _cached_land
    if decoded_tile is not _cached_land_tile or extent != _cached_land_extent:
        union = surface_water_union(decoded_tile, schema)
        square = buffered_square(extent, schema)
        if union is not None:
            # Forces GEOS's fixed-precision (snap-rounding) overlay instead
            # of the default floating-point one. The float overlay computes
            # edge intersections with ordinary float arithmetic; on geometry
            # with near-coincident or near-collinear points (buffer(0)
            # leftovers, water nearly touching the tile square) two edges
            # that should meet at one point can resolve to two float-apart
            # points instead, so the polygonizer can't match a hole ring to
            # its shell and throws (seen live on real OpenFreeMap water
            # geometry). Snap-rounding pre-snaps coordinates to a fixed
            # grid before computing intersections, so that inconsistency
            # can't arise.
            _cached_land = square.difference(union, grid_size=OUTPUT_GRID_SIZE)
        else:
            _cached_land = square
        _cached_land_tile = decoded_tile
        _cached_land_extent = extent
    return _cached_land
