"""Profile loader: a profile is loaded straight from its .py file's path.

No registration step, and no built-in case to special-case, since this
package ships no profiles of its own. The file MUST export a module-level
`PROFILE` naming its `Profile` subclass — the class, not an instance. Its
dependencies, if any, go in the same file, in a PEP 723 block
profile_requirements.py reads without importing it. See docs/PROFILES.md
("Writing and distributing your own profile").
"""
import importlib.util
from pathlib import Path


def load_profile(path):
    """Imports `path` as a standalone module and returns its `PROFILE` class.

    It MUST NOT be registered in sys.modules. transform.py's pool workers
    reload profiles by path (see `_transform_chunk()`), which works under
    both fork and spawn; registering would only help under fork."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"profile file not found: {path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "PROFILE"):
        raise ValueError(f"{path} has no module-level `PROFILE = YourProfileClass` assignment")
    return module.PROFILE
