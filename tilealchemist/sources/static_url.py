"""A plain, fixed PMTiles URL.

The escape hatch for local testing, and for any provider publishing one file
at a stable location with no "pick the latest build" convention to resolve.

The one source that MUST be told its schema. A bare URL could be anybody's
archive, so `--schema` says which. It arrives as a `TileSchema` like any
other source's, resolve_source() having looked the flag's `SchemaName` up in
`SCHEMAS`.
"""
from tilealchemist.sources.base import ResolvedSource, Source


class StaticUrlSource(Source):
    def __init__(self, url, schema):
        self.url = url
        self.schema = schema

    def resolve(self):
        return ResolvedSource(self.url, "n/a", self.schema)
