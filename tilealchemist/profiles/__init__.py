"""Profile loader: a profile is loaded straight from its .py file's path,
with no registration step and no built-in case to special-case, since this
package ships no profiles of its own. The file's one required export is a
module-level `PROFILE` naming its `Profile` subclass, the class rather than
an instance. Its dependencies, if any, go in that same file too, in a PEP
723 block that profile_requirements.py reads without importing it. See
docs/PROFILES.md ("Writing and distributing your own profile").
"""
import importlib.util
from pathlib import Path


def load_profile(path):
    """Imports `path` as its own standalone module and returns its
    module-level `PROFILE` class. Deliberately not registered in
    sys.modules: transform.py's pool workers reload profiles by path for
    that reason (see its `_transform_chunk()`), which works under both fork
    and spawn, whereas registering would only help under fork."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"profile file not found: {path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "PROFILE"):
        raise ValueError(f"{path} has no module-level `PROFILE = YourProfileClass` assignment")
    return module.PROFILE
