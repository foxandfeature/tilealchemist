"""What a built layer credits.

The source archive states its own attribution in the JSON metadata document
PMTiles v3 keeps at the header's `metadata_offset`, and the two sources this
repository ships do not state the same one:

    OpenFreeMap  OpenFreeMap, &copy; OpenMapTiles, &copy; OpenStreetMap contributors
    Protomaps    &copy; OpenStreetMap

A caller shapes that into the layer's own attribution with the pipeline's
`attribution` input, a template in which `{source}` stands for what the
archive declared. Whoever runs the pipeline owns this, not the profile: a
profile can be downloaded from anywhere, while the name to put in front of
the credit belongs to the run.

See docs/ARCHITECTURE.md ("Source attribution").
"""
import gzip
import json

from tilealchemist.ranged_fetch import fetch_range

# Names this module's ranged fetch in retry warnings (see ranged_fetch.py).
RETRY_LABEL = "prepare-shards metadata"

# What a template puts where the archive's own attribution goes.
PLACEHOLDER = "{source}"


def fetch_declared_attribution(session, url, header):
    """The attribution the archive states for itself, None if it states none.

    One ranged request of about a kilobyte. The metadata sits outside the
    16 KB prefix `pmtiles_index.py` already holds — Protomaps writes it at the
    end of a 137 GB archive — so it cannot be had for free.

    gzip is assumed rather than dispatched on the header's
    `internal_compression`. That value covers the directories too, and
    `deserialize_directory()` assumes gzip for those, so the walk this runs
    after has already failed on anything else.
    """
    length = header["metadata_length"]
    if length == 0:
        return None
    blob = fetch_range(session, url, header["metadata_offset"], length,
                       retry_label=RETRY_LABEL)
    document = json.loads(gzip.decompress(blob))
    attribution = document.get("attribution") if isinstance(document, dict) else None
    if not isinstance(attribution, str) or not attribution.strip():
        return None
    return attribution.strip()


def compose_attribution(declared, template):
    """The layer's attribution: `template` with `{source}` filled in.

    No template carries `declared` through unchanged, which is the common
    case. A template without the placeholder replaces it outright. The
    substitution is literal, which is why it happens here and not in the
    workflow's shell: `${v//p/r}` expands an unescaped `&` in the replacement
    to the match, and both sources' attributions are full of `&copy;`.

    A layer MUST NOT be published without an attribution, so every way of
    arriving at an empty one raises instead — in `prepare-shards`, before any
    worker is dispatched.
    """
    if not template:
        if not declared:
            raise ValueError(
                "the source archive declares no attribution of its own (no `attribution` "
                "key in its PMTiles metadata), so there is nothing to carry into the "
                "output; state it with the pipeline's `attribution` input")
        return declared

    if PLACEHOLDER in template and not declared:
        raise ValueError(
            f"the `attribution` template has a {PLACEHOLDER} to fill in, but the source "
            f"archive declares no attribution of its own; state the whole attribution "
            f"instead, without {PLACEHOLDER}")

    attribution = template.replace(PLACEHOLDER, declared or "").strip()
    if not attribution:
        raise ValueError(f"the `attribution` template {template!r} composes to nothing; a "
                         f"layer MUST carry an attribution")
    return attribution
