"""OpenFreeMap's planet PMTiles archive: a directory of timestamped builds,
not all fully published, so the latest usable one has to be picked out at
runtime rather than hardcoded.
"""
import re

import requests

from tilealchemist.sources.base import ResolvedSource, Source

FILES_URL = "https://btrfs.openfreemap.com/files.txt"
BASE_URL = "https://btrfs.openfreemap.com/"
PLANET_RE = re.compile(r"^areas/planet/(\d{8}_\d{6})_pt/(.+)$")


class OpenFreeMapSource(Source):
    def resolve(self):
        """See README.md ("Method") for why `done` + tiles.pmtiles, not just
        the newest directory listed."""
        response = requests.get(FILES_URL, timeout=30)
        response.raise_for_status()

        by_timestamp = {}
        for line in response.text.splitlines():
            match = PLANET_RE.match(line.strip())
            if match:
                by_timestamp.setdefault(match.group(1), set()).add(match.group(2))

        ready = [timestamp for timestamp, files in by_timestamp.items()
                 if "done" in files and "tiles.pmtiles" in files]
        if not ready:
            raise RuntimeError(f"no fully-published planet build found in {FILES_URL}")
        latest = max(ready)
        return ResolvedSource(f"{BASE_URL}areas/planet/{latest}_pt/tiles.pmtiles", latest)
