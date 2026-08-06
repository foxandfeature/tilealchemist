"""Profile registry: discovered as installable plugins via Python entry
points (group "tilealchemist.profiles"), not a hardcoded dict, so a package in
another repository can register its own profile through the exact same
mechanism tilealchemist's own `land`/`cropped-waterways` profiles use (see this
repo's own pyproject.toml). `build_shard.py --profile <name>` doesn't care
whether an entry point came from this package or an external one.
"""
from importlib.metadata import entry_points


def _discover_profiles():
    # entry_points(group=...) only exists from Python 3.10; on 3.9
    # entry_points() returns a plain {group_name: [EntryPoint, ...]} dict.
    all_entry_points = entry_points()
    if hasattr(all_entry_points, "select"):
        selected = all_entry_points.select(group="tilealchemist.profiles")
    else:
        selected = all_entry_points.get("tilealchemist.profiles", [])
    return {ep.name: ep.load() for ep in selected}


PROFILES = _discover_profiles()
