"""Project-scoped VectorCraft MCP adapter."""

from pathlib import Path

from ion_creative.adapters.base import BaseCraftAdapter
from ion_creative.paths import project_runtime_environment


class VectorCraftAdapter(BaseCraftAdapter):
    def __init__(self, project_root: Path):
        if project_root.is_symlink():
            raise ValueError("VectorCraft project root cannot be a symlink")
        root = project_root.expanduser().resolve()
        inputs = root / "inputs"
        if inputs.is_symlink():
            raise ValueError("VectorCraft input root cannot be a symlink")
        inputs.mkdir(parents=True, exist_ok=True)
        super().__init__(
            "vectorcraft",
            args=[
                "mcp",
                "--headless",
                "--automation-read-root",
                str(inputs),
                "--automation-write-root",
                str(root),
            ],
            cwd=root,
            project_root=root,
            env_overrides=project_runtime_environment(root, "vectorcraft"),
        )
