"""The vocabulary a profile works in: `Feature`, the thing it reads and
writes, and `FeatureSet`, the named kinds of source data it can ask for.

A `FeatureSet` is a name plus what that name *means*. It is deliberately tied
to no schema: `WATERWAYS` says "waterway line features", and every schema able
to answer that declares a `@feature(WATERWAYS)` method for it, however
differently its own layers happen to be structured. A profile imports the
feature sets it reads and stays schema-independent.

This is the one place a feature set's meaning is written down, the contract
both sides have to honour. Adding one for a domain no schema covers yet
(buildings, landuse, POIs) means an entry here plus a `@feature` method on the
schemas that can answer it, never a change to `TileSchema` or `Profile`, so no
existing schema or profile breaks.
"""
from dataclasses import dataclass, field

from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class Feature:
    """One feature: a shapely geometry plus the properties it carries.

    What `Tile.features()` hands a profile, and what `transform()` gives back.
    The raw {"geometry": ..., "properties": ...} dicts belong to
    `mapbox_vector_tile`, whose decoder produces them and whose encoder wants
    them back, and are kept at those two boundaries rather than being passed
    around as the currency of profile code.
    """

    geometry: BaseGeometry
    properties: dict = field(default_factory=dict)

    def with_geometry(self, geometry):
        """This feature with `geometry` in place of its own, properties carried
        through untouched. The common shape of a transform: cut, clip or
        simplify the geometry, keep what the source said about it."""
        return Feature(geometry, self.properties)


class FeatureSet:
    """One named kind of source data. Identity is the identity of the object,
    so a schema's `provides` mapping and a `Tile`'s memo keys can use it
    directly: two feature sets with the same name are still two feature
    sets."""

    def __init__(self, name, description):
        self.name = name
        self.description = description

    def __repr__(self):
        return f"<FeatureSet {self.name}>"


SURFACE_WATER = FeatureSet(
    "surface_water",
    "Real, non-tunnel surface water polygons: what a renderer would paint as "
    "open water. Water running through a tunnel is not part of this.")

WATERWAYS = FeatureSet(
    "waterways",
    "Waterway line features (rivers, streams, canals) with the schema's own "
    "properties preserved as-is.")
