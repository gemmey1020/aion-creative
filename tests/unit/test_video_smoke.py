import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from ion_creative.project import create_project

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
)
VALID_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42"


def _load_recipe(project_root: Path):
    recipe_path = project_root / "src" / "ion_creative" / "recipes" / "video_smoke.py"
    assert recipe_path.is_file(), "Ion video smoke recipe is not implemented"
    from ion_creative.recipes.video_smoke import run_video_smoke

    return run_video_smoke


def _content(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        structured_content=None,
    )


class FakeFilmSession:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.report = SimpleNamespace(
            status="HEALTHY",
            healthy=True,
            engine="filmcraft",
            version="0.7.2",
            tool_names=[
                "command_list",
                "command_run",
                "doc_inspect",
                "render_preview",
                "project_inspect",
                "sequence_inspect",
            ],
            tool_bindings={"command_run": "command_run", "render_preview": "render_preview"},
            preview_tool="render_preview",
            recipe_availability={"video_edit": True, "social_video": True},
            tool_schemas={
                "command_list": {
                    "properties": {
                        "filter": {"type": "string"},
                        "enabled_only": {"type": "boolean"},
                    }
                },
                "command_run": {
                    "properties": {"id": {"type": "string"}, "params": {"type": "object"}},
                    "required": ["id"],
                },
                "render_preview": {
                    "properties": {
                        "seconds": {"type": "number"},
                        "max_side": {"type": "integer"},
                    }
                },
            },
        )

    async def invoke_tool(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        if name == "project_inspect":
            return SimpleNamespace(
                ok=True,
                result=_content(json.dumps({"name": "FilmCraft Demo", "activeSequence": 21})),
                error_code=None,
                message="ok",
            )
        if name == "sequence_inspect":
            payload = {"id": 21, "video": [{"items": [{"clip": 42, "durationFrames": 50}]}]}
            return SimpleNamespace(
                ok=True,
                result=_content(json.dumps(payload)),
                error_code=None,
                message="ok",
            )
        if name == "command_list":
            query = str(arguments.get("filter", ""))
            commands = (
                [
                    {
                        "id": "live-trim-command",
                        "label": "Trim Edit",
                        "enabled": True,
                        "params": '{"clip":id,"edge":"in|out","mode":"regular|ripple","deltaFrames":i64}',
                    }
                ]
                if "trim" in query.casefold()
                else [
                    {
                        "id": "live-mp4-export-command",
                        "label": "Media…",
                        "enabled": True,
                        "params": '{"path":str,"format":"h264|hevc"?,"range":"entire|custom"?}',
                    }
                ]
            )
            return SimpleNamespace(
                ok=True,
                result=_content(json.dumps(commands)),
                error_code=None,
                message="ok",
            )
        if name == "render_preview":
            return SimpleNamespace(
                ok=True,
                result=SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="image",
                            mimeType="image/png",
                            data=base64.b64encode(VALID_PNG).decode(),
                        )
                    ]
                ),
                error_code=None,
                message="ok",
            )
        return SimpleNamespace(ok=False, result=None, error_code="UNKNOWN_TOOL", message=name)

    async def execute(self, request):
        self.calls.append(("command_run", request.arguments))
        command = request.arguments.get("id")
        if command == "live-mp4-export-command":
            destination = Path(str(request.arguments["params"]["path"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(VALID_MP4)
        return SimpleNamespace(ok=True, result=None, error_code=None, message="ok")

    async def preview(self, request):
        return await self.invoke_tool("render_preview", request.arguments)

    async def close(self):
        return None


class FakeFilmAdapter:
    def __init__(self, project_root: Path):
        self.session = FakeFilmSession(project_root)

    async def health(self):
        return SimpleNamespace(healthy=True, version="0.7.2", errors=[])

    async def open_session(self):
        return self.session


class FakeEffectSession:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.report = SimpleNamespace(
            tool_names=["render_frame"],
            tool_bindings={"render_frame": "render_frame"},
            recipe_availability={"image_effect": True},
            tool_schemas={"render_frame": {"properties": {"path": {"type": "string"}}}},
        )

    async def invoke_tool(self, name: str, arguments: dict[str, object]):
        if self.fail:
            return SimpleNamespace(
                ok=False,
                result=None,
                error_code="RENDER_FAILED",
                message="render-frame failure",
            )
        Path(str(arguments["path"])).write_bytes(VALID_PNG)
        return SimpleNamespace(ok=True, result=None, error_code=None, message="ok")

    async def close(self):
        return None


class FakeEffectAdapter:
    def __init__(self, *, fail: bool = False):
        self.session = FakeEffectSession(fail=fail)

    async def health(self):
        return SimpleNamespace(healthy=True, version="0.6.0", errors=[])

    async def open_session(self):
        return self.session


def test_video_smoke_discovers_timeline_and_export_commands_and_returns_review_candidate(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    run_video_smoke = _load_recipe(project_root)
    project = create_project(tmp_path / "video-project")
    film = FakeFilmAdapter(project.root)

    result = asyncio.run(
        run_video_smoke(project, film_adapter=film, effect_adapter=FakeEffectAdapter())
    )

    assert result.status == "READY_FOR_REVIEW"
    assert Path(result.preview_paths[0]).is_file()
    assert Path(result.candidate_paths[0]).is_file()
    assert Path(result.report_path).is_file()
    assert Path(result.candidate_paths[0]).read_bytes() == VALID_MP4
    assert Path(result.capabilities["effectcraft"]["frame_path"]).is_file()
    commands = [args for name, args in film.session.calls if name == "command_run"]
    assert [call["id"] for call in commands] == [
        "live-trim-command",
        "live-mp4-export-command",
    ]
    assert commands[0]["params"] == {
        "clip": 42,
        "edge": "out",
        "mode": "regular",
        "deltaFrames": -1,
    }




def test_effectcraft_failure_after_preview_keeps_filmcraft_candidate(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    run_video_smoke = _load_recipe(project_root)
    project = create_project(tmp_path / "video-project")
    film = FakeFilmAdapter(project.root)

    result = asyncio.run(
        run_video_smoke(
            project,
            film_adapter=film,
            effect_adapter=FakeEffectAdapter(fail=True),
        )
    )

    assert result.status == "PARTIAL_SUCCESS"
    assert Path(result.preview_paths[0]).is_file()
    assert Path(result.candidate_paths[0]).is_file()
    assert any(error["engine"] == "effectcraft" for error in result.errors)
    assert any(stage["stage"] == "preview" for stage in result.stages)
