"""Source contract: how to find the PMTiles archive to walk, and what schema
its tiles are in. Kept separate from the directory-walk/partition mechanics
behind prepare_shards.py (pmtiles_index.py, partition.py), which never need
to know how the URL was found, only what it resolved to.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from tilealchemist.schemas import TileSchema


@dataclass(frozen=True)
class ResolvedSource:
    url: str            # pmtiles URL to range-GET against
    build: str          # human-readable label for logs/source.json ("n/a" if not applicable)
    schema: TileSchema  # what the archive's tiles are in


class Source(ABC):
    # What this source's provider publishes, so `--source protomaps` can't be
    # paired with the wrong reader.
    schema: TileSchema

    @abstractmethod
    def resolve(self):
        """Called once by prepare_shards.py before the directory walk.
        Returns a ResolvedSource."""
