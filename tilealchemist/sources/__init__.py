"""Source registry: CLI-facing `--source` name -> `Source` class.

Sources are not a plugin surface, so a plain dict of this repo's built-ins
is the whole registry. Profiles differ: load_profile() resolves each one
from its .py path, with no dict at all.
"""
from tilealchemist.schemas import SCHEMAS
from tilealchemist.sources.openfreemap import OpenFreeMapSource
from tilealchemist.sources.protomaps import ProtomapsSource
from tilealchemist.sources.static_url import StaticUrlSource

SOURCES = {
    "openfreemap": OpenFreeMapSource,
    "protomaps": ProtomapsSource,
    "static-url": StaticUrlSource,
}


def resolve_source(source_name, source_url, schema=None):
    """The `Source` these CLI arguments name, not yet resolved.

    `schema` is the `--schema` value (a `SchemaName`, argparse having
    rejected anything else) or None. It belongs to static-url alone. Every
    other source declares its own, and a contradicting `--schema` MUST be
    refused rather than ignored."""
    if source_name == "static-url":
        if not source_url:
            raise ValueError("--source-url is required when --source static-url")
        if not schema:
            raise ValueError("--schema is required when --source static-url: a bare URL "
                             "could be any provider's archive, so nothing else can say "
                             "what schema its tiles are in")
        return StaticUrlSource(source_url, SCHEMAS[schema])

    source = SOURCES[source_name]()
    if schema is not None and schema != source.schema.name:
        raise ValueError(f"--source {source_name} publishes {source.schema.name} tiles, so "
                         f"--schema {schema} cannot be right; leave --schema off "
                         f"(only --source static-url needs one)")
    return source
