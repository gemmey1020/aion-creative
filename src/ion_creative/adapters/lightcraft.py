"""LightCraft MCP adapter."""

from pathlib import Path

from ion_creative.adapters.base import BaseCraftAdapter
from ion_creative.paths import project_runtime_environment


class LightCraftAdapter(BaseCraftAdapter):
    def __init__(self, *, demo: bool = False, project_root: Path | None = None):
        args = ["mcp"]
        if demo:
            args.append("--demo")
        root = project_root.expanduser().resolve() if project_root else None
        super().__init__(
            "lightcraft",
            args=args,
            cwd=root,
            project_root=root,
            env_overrides=(
                project_runtime_environment(root, "lightcraft") if root is not None else None
            ),
        )
