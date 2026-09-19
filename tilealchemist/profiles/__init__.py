"""Profile loader: a profile is loaded straight from its .py file's path."""
import importlib.util
from pathlib import Path


def load_profile(path):
    """Imports `path` as a standalone module and returns its `PROFILE` class."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"profile file not found: {path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    # Deliberately not registered in sys.modules: pool workers reload by path under spawn.
    spec.loader.exec_module(module)
    if not hasattr(module, "PROFILE"):
        raise ValueError(f"{path} has no module-level `PROFILE = YourProfileClass` assignment")
    return module.PROFILE
