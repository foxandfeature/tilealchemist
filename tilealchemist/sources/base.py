"""Source contract: how to find the PMTiles archive to walk. Kept separate
from prepare_shards.py's directory-walk/partition mechanics, which never
need to know how the URL was found, only what it resolved to.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedSource:
    url: str    # pmtiles URL to range-GET against
    build: str  # human-readable label for logs/source.json ("n/a" if not applicable)


class Source(ABC):
    @abstractmethod
    def resolve(self):
        """Called once by prepare_shards.py before the directory walk.
        Returns a ResolvedSource."""
