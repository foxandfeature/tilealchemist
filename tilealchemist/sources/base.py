"""Source contract: which PMTiles archive to walk, and what schema it is in."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from tilealchemist.schemas import TileSchema


@dataclass(frozen=True)
class ResolvedSource:
    url: str
    build: str  # Human-readable label for logs and source.json; "n/a" where there is none.
    schema: TileSchema


class Source(ABC):
    schema: TileSchema

    @abstractmethod
    def resolve(self):
        """Returns a ResolvedSource; called once, before the directory walk."""
