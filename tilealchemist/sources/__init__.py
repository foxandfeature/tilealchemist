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
