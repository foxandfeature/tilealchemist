"""Source registry: CLI-facing `--source` name -> `Source` class. Unlike
profiles (see profiles/__init__.py's load_profile(), which resolves every
profile straight from its .py path, no dict at all), sources have no
file-path escape hatch for a custom one: they aren't a plugin surface
external callers need to extend, so a plain dict of this repo's own
built-ins is the whole registry.
"""
from tilealchemist.sources.openfreemap import OpenFreeMapSource
from tilealchemist.sources.static_url import StaticUrlSource

SOURCES = {
    "openfreemap": OpenFreeMapSource,
    "static-url": StaticUrlSource,
}


def resolve_source(source_name, source_url):
    if source_name == "static-url":
        if not source_url:
            raise ValueError("--source-url is required when --source static-url")
        return StaticUrlSource(source_url)
    return SOURCES[source_name]()
