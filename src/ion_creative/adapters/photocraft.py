"""Project-scoped PhotoCraft MCP adapter."""

from pathlib import Path

from ion_creative.adapters.base import BaseCraftAdapter


class PhotoCraftAdapter(BaseCraftAdapter):
    def __init__(self, project_root: Path):
        root = project_root.expanduser().resolve()
        ion_directory = root / "ion"
        if ion_directory.is_symlink():
            raise ValueError("PhotoCraft's project-local runtime home cannot use a symlink")
        ion_directory.mkdir(parents=True, exist_ok=True)
        runtime_home = ion_directory / "photocraft-home"
        if runtime_home.is_symlink():
            raise ValueError("PhotoCraft's project-local runtime home cannot use a symlink")
        runtime_home.mkdir(parents=True, exist_ok=True)
        super().__init__(
            "photocraft",
            args=[
                "mcp",
                "--automation-read-root",
                str(root),
                "--automation-write-root",
                str(root),
            ],
            cwd=root,
            project_root=root,
            env_overrides={
                "HOME": str(runtime_home),
                "XDG_CONFIG_HOME": str(runtime_home / ".config"),
                "XDG_CACHE_HOME": str(runtime_home / ".cache"),
                "XDG_DATA_HOME": str(runtime_home / ".local" / "share"),
                "XDG_STATE_HOME": str(runtime_home / ".local" / "state"),
            },
        )
