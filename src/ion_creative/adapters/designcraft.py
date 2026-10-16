"""Disposable-sample DesignCraft MCP adapter."""

from pathlib import Path

from ion_creative.adapters.base import BaseCraftAdapter
from ion_creative.paths import project_runtime_environment


class DesignCraftAdapter(BaseCraftAdapter):
    def __init__(self, project_root: Path):
        if project_root.is_symlink():
            raise ValueError("DesignCraft project root cannot be a symlink")
        root = project_root.expanduser().resolve()
        super().__init__(
            "designcraft",
            args=["mcp", "--sample"],
            cwd=root,
            project_root=root,
            env_overrides=project_runtime_environment(root, "designcraft"),
        )
