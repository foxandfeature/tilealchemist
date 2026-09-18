"""Source contract: how to find the PMTiles archive to walk, and what schema
its tiles are in.

Kept apart from the walk and partition mechanics (pmtiles_index.py,
partition.py). Those never need to know how the URL was found, only what it
resolved to.
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
    # What this source's provider publishes. Pairing `--source protomaps`
    # with the wrong reader MUST NOT be possible.
    schema: TileSchema

    @abstractmethod
    def resolve(self):
        """Returns a ResolvedSource. Called once by prepare_shards.py, before
        the directory walk."""
