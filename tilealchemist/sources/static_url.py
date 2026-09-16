"""A plain, fixed PMTiles URL: the escape hatch for local testing and for
any other PMTiles provider that just publishes one file at a stable
location, with no "pick the latest build" convention to resolve.

The one source that has to be told its schema. Every other one knows, being
tied to a provider that publishes one; a bare URL could be anybody's archive,
so `--schema` says which, and says it here or nowhere. It arrives as a
`TileSchema` like any other source's, resolve_source() having already looked
the flag's `SchemaName` up in `SCHEMAS`.
"""
from tilealchemist.sources.base import ResolvedSource, Source


class StaticUrlSource(Source):
    def __init__(self, url, schema):
        self.url = url
        self.schema = schema

    def resolve(self):
        return ResolvedSource(self.url, "n/a", self.schema)
