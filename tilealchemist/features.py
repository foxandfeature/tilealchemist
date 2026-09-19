"""The vocabulary a profile works in; see docs/PROFILES.md."""
from dataclasses import dataclass, field

from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class Feature:
    geometry: BaseGeometry
    properties: dict = field(default_factory=dict)

    def with_geometry(self, geometry):
        return Feature(geometry, self.properties)


class FeatureSet:
    """One named kind of source data, identified by object identity."""

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
