import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from ion_creative.project import (
    create_project,
    export_finalized_artifact,
    finalize_artifact,
    sha256_file,
)

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
)


def _load_recipe(project_root: Path):
    module_path = project_root / "src" / "ion_creative" / "recipes" / "image_smoke.py"
    assert module_path.is_file(), "Ion image smoke recipe is not implemented"
    from ion_creative.recipes.image_smoke import run_image_smoke

    return run_image_smoke


class FakePhotoCraft:
    def __init__(self, project_root: Path, *, command_available: bool = True):
        self.project_root = project_root
        self.command_available = command_available
        self.calls: list[tuple[str, dict[str, object]]] = []
        tool_names = [
            "command_list",
            "command_run",
            "doc_open",
            "doc_export",
            "doc_render_preview",
        ]
        self.report = SimpleNamespace(
            status="HEALTHY",
            healthy=True,
            engine="photocraft",
            version="0.5.0",
            tool_names=tool_names,
            tool_bindings={"render_preview": "doc_render_preview"},
            preview_tool="doc_render_preview",
            tool_schemas={
                "doc_open": {
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "command_list": {
                    "properties": {
                        "filter": {"type": "string"},
                        "enabled_only": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "command_run": {
                    "properties": {"id": {"type": "string"}, "params": {"type": "object"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "doc_export": {
                    "properties": {
                        "path": {"type": "string"},
                        "format": {"type": "string"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "doc_render_preview": {
                    "properties": {"max_side": {"type": "integer"}},
                    "additionalProperties": False,
                },
            },
            missing_tools=[],
            recipe_availability={"image_edit": command_available},
            to_dict=lambda: {"engine": "photocraft", "tools": tool_names},
        )

    async def health(self):
        return SimpleNamespace(
            healthy=True,
            mcp_initialized=True,
            engine="photocraft",
            version="0.5.0",
            capabilities=self.report.tool_names,
            unavailable_recipes=[],
            errors=[],
            capability_report=self.report,
        )

    async def open_session(self):
        return self

    async def close(self):
        return None

    async def invoke_tool(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        if name == "command_list":
            commands = [
                {
                    "id": "runtime-discovered-invert",
                    "label": "Invert",
                    "menu": ["Image", "Adjustments"],
                    "params": "{}",
                    "enabled": self.command_available,
                }
            ]
            result = SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(commands))],
                structured_content=None,
            )
            return SimpleNamespace(ok=True, result=result, error_code=None, message="ok")
        if name == "doc_export":
            destination = self.project_root / str(arguments["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(VALID_PNG)
        return SimpleNamespace(ok=True, result=None, error_code=None, message="ok")

    async def execute(self, request):
        self.calls.append(("command_run", request.arguments))
        return SimpleNamespace(ok=True, result=None, error_code=None, message="ok")

    async def preview(self, request):
        self.calls.append(("render_preview", request.arguments))
        image = SimpleNamespace(type="image", mimeType="image/png", data=base64.b64encode(VALID_PNG).decode())
        return SimpleNamespace(
            ok=True,
            result=SimpleNamespace(content=[image]),
            error_code=None,
            message="ok",
        )


class FailedLightCraftStage:
    async def run(self, project, candidate):
        raise RuntimeError("LightCraft grade unavailable")


def _fixture_project(root: Path):
    project = create_project(root)
    source = project.inputs / "fixture.png"
    source.write_bytes(VALID_PNG)
    return project, source


def test_image_smoke_discovers_command_preserves_source_and_writes_preview_export(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    run_image_smoke = _load_recipe(project_root)
    project, source = _fixture_project(tmp_path / "ion-project")
    before = sha256_file(source)
    adapter = FakePhotoCraft(project.root)

    result = asyncio.run(run_image_smoke(project, photo_adapter=adapter))

    assert result.status == "READY_FOR_REVIEW"
    assert result.source_hash_before == before
    assert result.source_hash_after == before
    assert Path(result.preview_paths[0]).is_file()
    candidate = Path(result.candidate_paths[0])
    assert candidate.is_file()
    assert candidate.is_relative_to(project.intermediates)
    assert Path(result.report_path).is_file()
    assert ("command_run", {"id": "runtime-discovered-invert"}) in adapter.calls
    assert source.read_bytes() == VALID_PNG

    finalize_artifact(project, candidate)
    exported = export_finalized_artifact(project, candidate, "image-smoke.png")
    assert exported.is_file()
    assert exported.read_bytes() == VALID_PNG


def test_image_smoke_reports_unsupported_when_no_safe_edit_command_is_available(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    run_image_smoke = _load_recipe(project_root)
    project, _ = _fixture_project(tmp_path / "ion-project")
    adapter = FakePhotoCraft(project.root, command_available=False)

    result = asyncio.run(run_image_smoke(project, photo_adapter=adapter))

    assert result.status == "FAILED"
    assert any(error["code"] == "UNSUPPORTED_CAPABILITY" for error in result.errors)
    assert not list(project.exports.iterdir())


def test_lightcraft_failure_preserves_photo_export_and_prior_valid_output(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    run_image_smoke = _load_recipe(project_root)
    project, source = _fixture_project(tmp_path / "ion-project")
    old_export = project.exports / "image-smoke.png"
    old_export.write_bytes(VALID_PNG)
    old_bytes = old_export.read_bytes()
    adapter = FakePhotoCraft(project.root)

    result = asyncio.run(
        run_image_smoke(
            project,
            photo_adapter=adapter,
            lightcraft_stage=FailedLightCraftStage(),
        )
    )

    assert result.status == "PARTIAL_SUCCESS"
    assert Path(result.candidate_paths[0]).is_file()
    assert list(project.exports.glob("image-smoke*.png")) == [old_export]
    assert old_export.read_bytes() == old_bytes
    assert sha256_file(source) == result.source_hash_before
    assert any(error["engine"] == "lightcraft" for error in result.errors)
