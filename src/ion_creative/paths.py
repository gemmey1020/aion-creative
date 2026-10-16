"""User-space runtime paths for Ion Creative."""

import os
from pathlib import Path


def data_root() -> Path:
    """Return the user-owned Ion data root."""
    return Path.home() / ".local" / "share" / "ion-creative"


def runtime_root() -> Path:
    """Return the runtime registry root, with a testable explicit override."""
    configured = os.environ.get("ION_CREATIVE_RUNTIME_ROOT")
    return Path(configured).expanduser() if configured else data_root() / "runtimes"


def runtime_dir(engine: str, version: str) -> Path:
    """Return one isolated engine/version directory."""
    return runtime_root() / engine / version


def project_runtime_environment(project_root: Path, engine: str) -> dict[str, str]:
    """Return project-scoped HOME/XDG paths for disposable headless workflows."""
    root = project_root.expanduser().resolve()
    ion_directory = root / "ion"
    if ion_directory.is_symlink():
        raise ValueError("Project runtime home cannot be placed through an ion symlink")
    ion_directory.mkdir(parents=True, exist_ok=True)
    runtime_home = ion_directory / f"{engine}-home"
    if runtime_home.is_symlink():
        raise ValueError("Project runtime home cannot be a symlink")
    runtime_home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(runtime_home),
        "XDG_CONFIG_HOME": str(runtime_home / ".config"),
        "XDG_CACHE_HOME": str(runtime_home / ".cache"),
        "XDG_DATA_HOME": str(runtime_home / ".local" / "share"),
        "XDG_STATE_HOME": str(runtime_home / ".local" / "state"),
    }
