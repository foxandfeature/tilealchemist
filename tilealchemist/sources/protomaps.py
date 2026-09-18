"""Protomaps' daily planet basemap builds.

A bucket of dated PMTiles files with a JSON index listing them, and a
retention window short enough that no particular build can be named ahead of
time.

Unlike OpenFreeMap's directory listing, the index carries no "this one is
finished" marker and needs none. It is generated from the bucket's object
listing, and an object appears there only once its upload has completed.

See docs/ARCHITECTURE.md "Source resolution" for the rest, including what
Protomaps asks of anyone reading these URLs.
"""
import requests

from tilealchemist.schemas import PROTOMAPS
from tilealchemist.sources.base import ResolvedSource, Source

BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
BASE_URL = "https://build.protomaps.com/"


class ProtomapsSource(Source):
    schema = PROTOMAPS

    def resolve(self):
        """The newest dated build in the index.

        The index also keeps the last build of each older basemap version, so
        "newest" MUST mean the build date in the key, not the position in the
        list. The freshest OSM data is the point of re-resolving every run."""
        response = requests.get(BUILDS_URL, timeout=30)
        response.raise_for_status()

        builds = [build for build in response.json()
                  if str(build.get("key", "")).endswith(".pmtiles")]
        if not builds:
            raise RuntimeError(f"no .pmtiles build listed in {BUILDS_URL}")

        latest = max(builds, key=lambda build: build["key"])
        version = latest.get("version", "unknown")
        return ResolvedSource(BASE_URL + latest["key"],
                              f"{latest['key'].removesuffix('.pmtiles')} (basemap {version})",
                              self.schema)
