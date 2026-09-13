"""A plain, fixed PMTiles URL: the escape hatch for local testing and for
any other PMTiles provider that just publishes one file at a stable
location, with no "pick the latest build" convention to resolve.
"""
from tilealchemist.sources.base import ResolvedSource, Source


class StaticUrlSource(Source):
    def __init__(self, url):
        self.url = url

    def resolve(self):
        return ResolvedSource(self.url, "n/a")
