"""A plain, fixed PMTiles URL; the one source that must be told its schema."""
from tilealchemist.sources.base import ResolvedSource, Source


class StaticUrlSource(Source):
    def __init__(self, url, schema):
        self.url = url
        self.schema = schema

    def resolve(self):
        return ResolvedSource(self.url, "n/a", self.schema)
