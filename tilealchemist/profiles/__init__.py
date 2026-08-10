"""Profile loader: every profile, this repo's own `land`/`cropped-waterways`
included, is loaded straight from its .py file's path, with no separate
registration step. That file's one required export is a module-level
`PROFILE` naming its `Profile` subclass (the class itself, not an instance:
`build_shard.py` instantiates it itself with the chosen schema). A profile's
own dependencies (e.g. shapely) belong in a requirements.txt next to its
.py file: see _pipeline.yml's "Install tilealchemist" step in the
build-shards job, which pip installs it before this module ever runs.
"""
import importlib.util
import os


def load_profile(path):
    """Imports `path` as its own standalone module and returns its
    module-level `PROFILE` class."""
    if not os.path.isfile(path):
        raise ValueError(f"profile file not found: {path}")
    module_name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "PROFILE"):
        raise ValueError(f"{path} has no module-level `PROFILE = YourProfileClass` assignment")
    return module.PROFILE
