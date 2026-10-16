import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

EXPECTED = {
    "inspect_asset",
    "inspect_runtime",
    "plan_workflow",
    "list_capabilities",
    "run_capability",
    "run_capability_batch",
    "run_workflow",
    "preview",
    "edit_image",
    "grade_image",
    "edit_video",
    "grade_video",
    "make_social_video",
    "finalize",
    "export",
}


def test_server_exposes_the_approved_high_level_tools() -> None:
    project_root = Path(__file__).resolve().parents[2]
    server_path = project_root / "src" / "ion_creative" / "server.py"
    assert server_path.is_file(), "Ion Creative MCP server is not implemented"

    from mcp import Client

    from ion_creative.server import create_server

    async def list_tool_names() -> set[str]:
        async with Client(create_server()) as client:
            result = await client.list_tools()
        return {tool.name for tool in result.tools}

    assert asyncio.run(list_tool_names()) == EXPECTED


def test_list_capabilities_returns_live_normalized_records(monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server
    from ion_creative.adapters.base import CapabilityReport, ToolDescriptor

    class FakeAdapter:
        async def discover(self):
            tool = ToolDescriptor(
                "edit.exposure",
                "Adjust image exposure.",
                {"type": "object", "properties": {"stops": {"type": "number"}}},
            )
            return CapabilityReport(
                engine="photocraft",
                version="0.5.0",
                status="HEALTHY",
                tool_names=[tool.name],
                tools=[tool],
            )

    monkeypatch.setattr(server, "_runtime_adapter", lambda engine, project_root: FakeAdapter())

    async def invoke_catalog():
        async with Client(server.create_server()) as client:
            return await client.call_tool("list_capabilities", {"engine": "photocraft"})

    result = asyncio.run(invoke_catalog())

    assert result.structured_content["status"] == "HEALTHY"
    capability = result.structured_content["capabilities"][0]
    assert capability["engine"] == "photocraft"
    assert capability["raw_name"] == "edit.exposure"
    assert capability["capability_id"] == "photocraft.edit_exposure"
    assert capability["category"] == "color/develop"
    assert capability["supports_preview"] is False
    assert capability["supports_export"] is False


def test_list_capabilities_applies_all_requested_filters(monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server
    from ion_creative.adapters.base import CapabilityReport, ToolDescriptor

    class FakeAdapter:
        async def discover(self):
            tools = [
                ToolDescriptor("edit.exposure", "Adjust image exposure", {"type": "object"}),
                ToolDescriptor("doc_render_preview", "Render a preview", {"type": "object"}),
                ToolDescriptor("file_export_png", "Export PNG", {"type": "object"}),
            ]
            return CapabilityReport(
                engine="photocraft",
                version="0.5.0",
                status="HEALTHY",
                tool_names=[tool.name for tool in tools],
                tools=tools,
                preview_tool="doc_render_preview",
            )

    monkeypatch.setattr(server, "_runtime_adapter", lambda engine, project_root: FakeAdapter())

    async def invoke():
        async with Client(server.create_server()) as client:
            return await client.call_tool(
                "list_capabilities",
                {
                    "engine": "photocraft",
                    "category": "import/export",
                    "search_query": "png",
                    "supports_preview": False,
                    "supports_export": True,
                },
            )

    result = asyncio.run(invoke())

    assert [item["raw_name"] for item in result.structured_content["capabilities"]] == [
        "file_export_png"
    ]
    assert result.structured_content["engines"][0]["capability_count"] == 1


def _install_capability_adapter(monkeypatch, tmp_path: Path, *, status: str = "HEALTHY"):
    from ion_creative import server
    from ion_creative.adapters.base import AdapterResult, CapabilityReport, ToolDescriptor

    calls: list[tuple[str, dict[str, object]]] = []
    sessions = []
    command_list = ToolDescriptor(
        "command_list",
        "Return currently available command IDs.",
        {"type": "object", "properties": {}},
    )
    tool = ToolDescriptor(
        "command_run",
        "Run a live-discovered Craft command.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "path": {"type": "string"},
                "params": {"type": "object", "additionalProperties": True},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    )

    class FakeSession:
        report = CapabilityReport(
            engine="photocraft",
            version="0.5.0",
            status=status,
            tool_names=[command_list.name, tool.name],
            tools=[command_list, tool],
            tool_bindings={"command_list": command_list.name},
        )

        def __init__(self):
            self.owner_task = asyncio.current_task()
            self.close_called = False
            sessions.append(self)

        async def invoke_tool(self, name: str, arguments: dict[str, object]):
            if name == "command_list":
                payload = {
                    "commands": [
                        (
                            {
                                "id": command_id,
                                "schema": {
                                    "type": "object",
                                    "properties": {"stops": {"type": "number"}},
                                },
                            }
                            if command_id == "edit.exposure"
                            else {"id": command_id}
                        )
                        for command_id in (
                            "edit.exposure",
                            "file.open",
                            "file.save",
                            "first",
                            "fail",
                            "last",
                        )
                    ]
                }
                return AdapterResult(
                    engine="photocraft",
                    tool=name,
                    ok=True,
                    result=SimpleNamespace(
                        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                        structured_content=None,
                    ),
                    message="discovered",
                )
            calls.append((name, arguments))
            ok = arguments.get("id") != "fail"
            return AdapterResult(
                engine="photocraft",
                tool=name,
                ok=ok,
                error_code=None if ok else "TOOL_CALL_FAILED",
                message="done" if ok else "operation failed",
            )

        async def close(self):
            assert asyncio.current_task() is self.owner_task, (
                "MCP session must close in the task that opened it"
            )
            self.close_called = True

    class FakeAdapter:
        def __init__(self, engine: str, project_root: Path):
            self.project_root = project_root

        async def open_session(self):
            return FakeSession()

        async def discover(self):
            return FakeSession.report

    monkeypatch.setenv("ION_CREATIVE_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setattr(server, "_runtime_adapter", lambda engine, project_root: FakeAdapter(engine, project_root))
    return calls, sessions


def test_run_capability_executes_a_live_capability_in_a_named_project(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative.server import create_server

    calls, _ = _install_capability_adapter(monkeypatch, tmp_path)

    async def invoke():
        async with Client(create_server()) as client:
            return await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {"id": "edit.exposure"},
                    "project_id": "brand-probe",
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "SUCCESS"
    assert result["raw_name"] == "command_run"
    assert result["project_id"] == "brand-probe"
    assert calls == [("command_run", {"id": "edit.exposure"})]
    assert (tmp_path / "projects" / "brand-probe" / "inputs").is_dir()


def test_run_capability_rejects_unknown_ids_and_unhealthy_engines(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative.server import create_server

    calls, _ = _install_capability_adapter(monkeypatch, tmp_path)

    async def invoke_unknown():
        async with Client(create_server()) as client:
            return await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.not_real",
                    "arguments": {},
                },
            )

    unknown = asyncio.run(invoke_unknown()).structured_content
    unavailable_calls, _ = _install_capability_adapter(monkeypatch, tmp_path, status="DEGRADED")

    async def invoke_unavailable():
        async with Client(create_server()) as client:
            return await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {"id": "edit.exposure"},
                    "project_id": "unhealthy",
                },
            )

    unavailable = asyncio.run(invoke_unavailable()).structured_content

    assert unknown["status"] == "UNSUPPORTED_CAPABILITY"
    assert unavailable["status"] == "UNAVAILABLE"
    assert calls == []
    assert unavailable_calls == []


def test_run_capability_rejects_paths_outside_the_project(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative.server import create_server

    calls, _ = _install_capability_adapter(monkeypatch, tmp_path)

    async def invoke():
        async with Client(create_server()) as client:
            return await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {"id": "file.open", "path": "/etc/passwd"},
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "FAILED"
    assert result["errors"][0]["code"] == "PROJECT_PATH_REJECTED"
    assert calls == []


def test_run_capability_maps_input_and_output_paths_into_project_areas(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative.project import create_project, sha256_file
    from ion_creative.server import create_server

    calls, _ = _install_capability_adapter(monkeypatch, tmp_path)
    project = create_project(tmp_path / "projects" / "path-probe")
    source = project.inputs / "source.png"
    source.write_bytes(b"disposable input")
    source_hash = sha256_file(source)

    async def invoke():
        async with Client(create_server()) as client:
            opened = await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {"id": "file.open", "params": {"path": "source.png"}},
                    "project_id": "path-probe",
                },
            )
            saved = await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {
                        "id": "file.save",
                        "params": {"path": "exports/candidate.pcraft"},
                    },
                    "project_id": "path-probe",
                },
            )
        return opened.structured_content, saved.structured_content

    opened, saved = asyncio.run(invoke())

    assert opened["status"] == "SUCCESS"
    assert saved["status"] == "SUCCESS"
    assert calls[0][1]["params"]["path"] == str(source)
    assert calls[1][1]["params"]["path"] == str(project.exports / "candidate.pcraft")
    assert sha256_file(source) == source_hash


def test_direct_capability_path_direction_uses_the_live_tool_name(tmp_path) -> None:
    from ion_creative.project import ProjectSafetyError, create_project
    from ion_creative.server import _confine_capability_arguments, _is_filesystem_path_key

    project = create_project(tmp_path / "direct-path-project")
    source = project.inputs / "source.png"
    source.write_bytes(b"read only")

    opened = _confine_capability_arguments(project, "doc_open", {"path": "source.png"})
    saved = _confine_capability_arguments(project, "doc_save", {"path": "candidate.pcraft"})
    property_path = _confine_capability_arguments(
        project,
        "command_run",
        {"id": "property.set", "params": {"path": "transform.position"}},
    )
    design_save = _confine_capability_arguments(
        project,
        "execute",
        {"command": "file.save", "params": {"path": "exports/layout.designcraft"}},
    )
    design_batch = _confine_capability_arguments(
        project,
        "batch",
        {
            "commands": [
                {"command": "file.save", "params": {"path": "exports/batch.designcraft"}}
            ]
        },
    )
    source_file = _confine_capability_arguments(
        project, "file.open", {"source_file": "source.png"}
    )
    output_file = _confine_capability_arguments(
        project, "file.export", {"output_file": "candidate.png"}
    )

    assert opened["path"] == str(source)
    assert saved["path"] == str(project.working / "candidate.pcraft")
    assert property_path["params"]["path"] == "transform.position"
    assert design_save["params"]["path"] == str(project.exports / "layout.designcraft")
    assert design_batch["commands"][0]["params"]["path"] == str(
        project.exports / "batch.designcraft"
    )
    assert source_file["source_file"] == str(source)
    assert output_file["output_file"] == str(project.exports / "candidate.png")
    assert _is_filesystem_path_key("user_profile") is False

    with pytest.raises(ProjectSafetyError):
        _confine_capability_arguments(
            project, "file.open", {"source_file": "/tmp/outside.png"}
        )


def test_run_capability_requires_nested_command_to_be_live_discovered(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative.server import create_server

    calls, _ = _install_capability_adapter(monkeypatch, tmp_path)

    async def invoke():
        async with Client(create_server()) as client:
            return await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {"id": "guess.command"},
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "INVALID_ARGUMENTS"
    assert result["errors"][0]["code"] == "COMMAND_NOT_DISCOVERED"
    assert calls == []


def test_run_capability_validates_nested_params_when_live_schema_exists(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative.server import create_server

    calls, _ = _install_capability_adapter(monkeypatch, tmp_path)

    async def invoke():
        async with Client(create_server()) as client:
            return await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {
                        "id": "edit.exposure",
                        "params": {"stops": "too bright"},
                    },
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "INVALID_ARGUMENTS"
    assert result["errors"][0]["code"] == "INVALID_COMMAND_ARGUMENTS"
    assert calls == []


def test_designcraft_batch_command_ids_are_checked_against_live_listing() -> None:
    from ion_creative.adapters.base import AdapterResult, CapabilityReport, ToolDescriptor
    from ion_creative.server import _validate_live_command_ids

    list_tool = ToolDescriptor("list_commands", "List live DesignCraft commands", {"type": "object"})
    batch_tool = ToolDescriptor("batch", "Run ordered commands", {"type": "object"})

    class FakeSession:
        report = CapabilityReport(
            engine="designcraft",
            version="0.6.0",
            status="HEALTHY",
            tool_names=[list_tool.name, batch_tool.name],
            tools=[list_tool, batch_tool],
            tool_bindings={"command_list": list_tool.name, "command_batch": batch_tool.name},
        )

        async def invoke_tool(self, name: str, arguments: dict[str, object]):
            payload = {"commands": [{"id": "file.save"}]}
            return AdapterResult(
                engine="designcraft",
                tool=name,
                ok=True,
                result=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=json.dumps(payload))]
                ),
            )

    valid, message = asyncio.run(
        _validate_live_command_ids(
            FakeSession(),
            "batch",
            {"commands": [{"command": "file.save", "params": {"path": "candidate"}}]},
        )
    )

    assert valid is True
    assert message == ""


def test_run_capability_preview_and_export_flags_return_reviewable_state(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server
    from ion_creative.adapters.base import AdapterResult, CapabilityReport, ToolDescriptor

    png_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
    )
    command = ToolDescriptor("command_run", "Run a command", {"type": "object", "properties": {}})
    preview = ToolDescriptor("render_preview", "Render preview", {"type": "object", "properties": {}})

    class FakeSession:
        report = CapabilityReport(
            engine="photocraft",
            version="0.5.0",
            status="HEALTHY",
            tool_names=[command.name, preview.name],
            tools=[command, preview],
            preview_tool=preview.name,
        )

        async def invoke_tool(self, name: str, arguments: dict[str, object]):
            content = (
                [SimpleNamespace(type="image", data=png_base64, mime_type="image/png")]
                if name == "render_preview"
                else []
            )
            return AdapterResult(
                engine="photocraft",
                tool=name,
                ok=True,
                result=SimpleNamespace(content=content, structured_content=None),
                message="done",
            )

        async def close(self):
            return None

    class FakeAdapter:
        async def open_session(self):
            return FakeSession()

    monkeypatch.setenv("ION_CREATIVE_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setattr(server, "_runtime_adapter", lambda engine, project_root: FakeAdapter())

    async def invoke():
        async with Client(server.create_server()) as client:
            return await client.call_tool(
                "run_capability",
                {
                    "engine": "photocraft",
                    "capability_id": "photocraft.command_run",
                    "arguments": {},
                    "project_id": "review-probe",
                    "preview_after": True,
                    "export_after": True,
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "SUCCESS"
    assert result["preview"]["status"] == "SUCCESS"
    assert Path(result["preview"]["path"]).is_file()
    assert result["export"]["status"] == "REQUIRES_FINALIZE"


def test_run_capability_batch_returns_structured_partial_failure(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative.server import create_server

    calls, sessions = _install_capability_adapter(monkeypatch, tmp_path)

    async def invoke():
        async with Client(create_server()) as client:
            return await client.call_tool(
                "run_capability_batch",
                {
                    "project_id": "batch-probe",
                    "stop_on_error": False,
                    "steps": [
                        {
                            "engine": "photocraft",
                            "capability_id": "photocraft.command_run",
                            "arguments": {"id": "first"},
                        },
                        {
                            "engine": "photocraft",
                            "capability_id": "photocraft.command_run",
                            "arguments": {"id": "fail"},
                        },
                        {
                            "engine": "photocraft",
                            "capability_id": "photocraft.command_run",
                            "arguments": {"id": "last"},
                        },
                    ],
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "PARTIAL_SUCCESS"
    assert [step["status"] for step in result["steps"]] == [
        "SUCCESS",
        "FAILED",
        "SUCCESS",
    ]
    assert [arguments["id"] for _, arguments in calls] == ["first", "fail", "last"]
    assert len(sessions) == 1
    assert all(session.close_called for session in sessions)


def test_codex_local_stdio_plugin_uses_a_plugin_root_relative_working_directory() -> None:
    project_root = Path(__file__).resolve().parents[2]
    manifest = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
    ion_server = manifest["mcpServers"]["ion-creative"]

    assert ion_server["cwd"] == "."
    assert ion_server["args"] == ["run", "--locked", "ion-creative-mcp"]


def test_video_preview_reuses_the_matching_review_frame(tmp_path: Path) -> None:
    from ion_creative.project import create_project
    from ion_creative.server import _locate_video_preview

    project = create_project(tmp_path / "video-project")
    artifact = project.intermediates / "candidate.mp4"
    artifact.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    preview = project.previews / "candidate-frame.png"
    preview.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    )
    (project.ion / "video-smoke-report.json").write_text(
        '{"recipe":"video_smoke","status":"READY_FOR_REVIEW",'
        f'"candidate_paths":["{artifact}"],"preview_paths":["{preview}"],'
        '"engines_used":["filmcraft"],"exact_versions":{"filmcraft":"0.7.2"}}',
        encoding="utf-8",
    )

    result = _locate_video_preview(project, artifact)

    assert result is not None
    assert result.status == "SUCCESS"
    assert result.preview_paths == [str(preview)]
    assert result.engines == ["filmcraft"]


def test_video_preview_ignores_malformed_guided_receipt_paths(tmp_path: Path) -> None:
    from ion_creative.project import create_project
    from ion_creative.server import _locate_video_preview

    project = create_project(tmp_path / "malformed-video-project")
    artifact = project.intermediates / "candidate.mp4"
    artifact.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (project.ion / "social-video-report.json").write_text(
        '{"recipe":"social_video","status":"READY_FOR_REVIEW",'
        '"candidate_paths":[42],"preview_paths":[null]}',
        encoding="utf-8",
    )

    assert _locate_video_preview(project, artifact) is None


def test_social_video_report_replaces_hardlink_without_mutating_external_target(tmp_path: Path) -> None:
    import json

    from ion_creative.project import create_project
    from ion_creative.workflows.social_video import _write_social_video_report

    project = create_project(tmp_path / "social-video-project")
    external_file = tmp_path / "external-report.json"
    original = json.dumps({"recipe": "social_video", "status": "READY_FOR_REVIEW"})
    external_file.write_text(original, encoding="utf-8")
    report_path = project.ion / "social-video-report.json"
    report_path.hardlink_to(external_file)
    candidate = project.intermediates / "candidate.mp4"
    preview = project.previews / "frame.png"

    _write_social_video_report(project, candidate, preview, "0.7.2")

    assert external_file.read_text(encoding="utf-8") == original
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["candidate_paths"] == [str(candidate)]


def test_unverified_video_source_and_grade_requests_return_structured_unsupported_status() -> None:
    from mcp import Client

    from ion_creative.server import create_server

    async def invoke_unsupported_tools():
        async with Client(create_server()) as client:
            source_edit = await client.call_tool(
                "edit_video",
                {
                    "project_root": "/tmp/unused-project",
                    "source_paths": ["/tmp/user-media.mp4"],
                    "instructions": "trim it",
                },
            )
            grade = await client.call_tool(
                "grade_video",
                {
                    "project_root": "/tmp/unused-project",
                    "source_path": "/tmp/user-media.mp4",
                    "look": "cinematic",
                },
            )
            social = await client.call_tool(
                "make_social_video",
                {
                    "project_root": "/tmp/unused-project",
                    "source_paths": ["/tmp/user-media.mp4"],
                    "platform": "instagram",
                },
            )
        return source_edit.structured_content, grade.structured_content, social.structured_content

    results = asyncio.run(invoke_unsupported_tools())

    assert [result["status"] for result in results] == [
        "UNSUPPORTED_CAPABILITY",
        "UNSUPPORTED_CAPABILITY",
        "UNSUPPORTED_CAPABILITY",
    ]


def _fake_recipe_result(status: str = "READY_FOR_REVIEW") -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        errors=[],
        candidate_paths=["/tmp/candidate"],
        output_paths=[],
        engines_used=["photocraft" if status != "VIDEO" else "filmcraft"],
        exact_versions={"photocraft": "0.5.0"},
        preview_paths=["/tmp/preview.png"],
        qa_needed=True,
    )


def _install_fake_adapters(monkeypatch, states: dict[str, tuple[bool, str]]) -> list[str]:
    from ion_creative import server

    calls: list[str] = []
    adapter_symbols = {
        "photocraft": "PhotoCraftAdapter",
        "filmcraft": "FilmCraftAdapter",
        "effectcraft": "EffectCraftAdapter",
        "lightcraft": "LightCraftAdapter",
        "vectorcraft": "VectorCraftAdapter",
        "designcraft": "DesignCraftAdapter",
        "pdfcraft": "PdfCraftAdapter",
    }

    def fake_adapter(engine: str, healthy: bool, error: str):
        class FakeAdapter:
            def __init__(self, *args, **kwargs):
                pass

            async def health(self):
                calls.append(engine)
                return SimpleNamespace(
                    engine=engine,
                    version={
                        "photocraft": "0.5.0",
                        "filmcraft": "0.7.2",
                        "effectcraft": "0.6.0",
                        "lightcraft": "0.6.0",
                        "vectorcraft": "0.10.0",
                        "designcraft": "0.6.0",
                        "pdfcraft": "0.7.1",
                    }[engine],
                    healthy=healthy,
                    capabilities=[f"{engine}.verified"],
                    errors=[error] if error else [],
                )

        FakeAdapter.__name__ = f"Fake{engine.title()}Adapter"
        return FakeAdapter

    for engine, symbol in adapter_symbols.items():
        healthy, error = states.get(engine, (True, ""))
        monkeypatch.setattr(
            server,
            symbol,
            fake_adapter(engine, healthy, error),
            raising=False,
        )
    return calls


def test_edit_image_refuses_to_start_when_photocraft_is_unhealthy(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server

    _install_fake_adapters(monkeypatch, {"photocraft": (False, "MCP unavailable")})
    workflow_calls: list[str] = []

    async def image_recipe(*args, **kwargs):
        workflow_calls.append("run")
        return _fake_recipe_result()

    monkeypatch.setattr(server, "run_image_smoke", image_recipe)

    async def invoke():
        async with Client(server.create_server()) as client:
            return await client.call_tool(
                "edit_image",
                {
                    "project_root": str(tmp_path / "untouched-project"),
                    "source_path": "/tmp/disposable.png",
                    "instructions": "smoke",
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "DEGRADED"
    assert result["engines"] == ["photocraft"]
    assert result["exact_versions"] == {"photocraft": "0.5.0"}
    assert result["errors"] == [
        {
            "engine": "photocraft",
            "stage": "health",
            "code": "ENGINE_HEALTH_CHECK_FAILED",
            "message": "MCP unavailable",
        }
    ]
    assert workflow_calls == []
    assert not (tmp_path / "untouched-project").exists()


def test_edit_video_refuses_to_start_when_filmcraft_is_unhealthy(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server

    _install_fake_adapters(monkeypatch, {"filmcraft": (False, "demo session unavailable")})
    workflow_calls: list[str] = []

    async def video_recipe(*args, **kwargs):
        workflow_calls.append("run")
        return _fake_recipe_result()

    monkeypatch.setattr(server, "run_video_smoke", video_recipe)

    async def invoke():
        async with Client(server.create_server()) as client:
            return await client.call_tool(
                "edit_video",
                {
                    "project_root": str(tmp_path / "untouched-project"),
                    "source_paths": [],
                    "instructions": "smoke",
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "DEGRADED"
    assert result["engines"] == ["filmcraft"]
    assert result["exact_versions"] == {"filmcraft": "0.7.2"}
    assert result["errors"] == [
        {
            "engine": "filmcraft",
            "stage": "health",
            "code": "ENGINE_HEALTH_CHECK_FAILED",
            "message": "demo session unavailable",
        }
    ]
    assert workflow_calls == []
    assert not (tmp_path / "untouched-project").exists()


def test_inspect_runtime_reports_all_live_engines_and_capabilities(monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server
    from ion_creative.desktop import launchers

    calls = _install_fake_adapters(monkeypatch, {"lightcraft": (False, "MCP list failed")})
    monkeypatch.setattr(
        launchers,
        "verify_launchers",
        lambda: [
            {
                "engine": engine,
                "installed": engine != "lightcraft",
                "verified": engine != "lightcraft",
                "error_code": "MISSING_LAUNCHER" if engine == "lightcraft" else None,
            }
            for engine in server.RUNTIME_ENGINES
        ],
    )

    async def invoke():
        async with Client(server.create_server()) as client:
            return await client.call_tool("inspect_runtime", {})

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "DEGRADED"
    assert [engine["engine"] for engine in result["engines"]] == [
        "photocraft",
        "filmcraft",
        "effectcraft",
        "lightcraft",
        "vectorcraft",
        "designcraft",
        "pdfcraft",
    ]
    assert [engine["healthy"] for engine in result["engines"]] == [
        True,
        True,
        True,
        False,
        True,
        True,
        True,
    ]
    assert [engine["capability_count"] for engine in result["engines"]] == [
        len(engine["capabilities"]) for engine in result["engines"]
    ]
    assert next(
        engine["launcher_state"] for engine in result["engines"] if engine["engine"] == "photocraft"
    ) == "VERIFIED"
    assert next(
        engine["launcher_state"] for engine in result["engines"] if engine["engine"] == "lightcraft"
    ) == "MISSING"
    light_inspection = next(item for item in result["engines"] if item["engine"] == "lightcraft")
    assert light_inspection["capabilities"] == ["lightcraft.verified"]
    assert light_inspection["errors"] == [
        {
            "engine": "lightcraft",
            "stage": "health",
            "code": "ENGINE_HEALTH_CHECK_FAILED",
            "message": "MCP list failed",
        }
    ]
    assert calls == [
        "photocraft",
        "filmcraft",
        "effectcraft",
        "lightcraft",
        "vectorcraft",
        "designcraft",
        "pdfcraft",
    ]


def test_image_recipe_errors_keep_their_machine_readable_fields(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server

    _install_fake_adapters(monkeypatch, {})
    expected_error = {
        "engine": "photocraft",
        "stage": "preview",
        "code": "INVALID_PREVIEW",
        "message": "PhotoCraft returned no valid PNG preview",
    }

    async def image_recipe(*args, **kwargs):
        result = _fake_recipe_result("FAILED")
        result.errors = [expected_error]
        return result

    monkeypatch.setattr(server, "run_image_smoke", image_recipe)

    async def invoke():
        async with Client(server.create_server()) as client:
            return await client.call_tool(
                "edit_image",
                {
                    "project_root": str(tmp_path / "image-project"),
                    "source_path": "/tmp/disposable.png",
                    "instructions": "smoke",
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["errors"] == [expected_error]


def test_preview_reports_a_structured_error_when_health_has_no_details(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server
    from ion_creative.project import create_project

    class UnhealthyPhotoCraftAdapter:
        def __init__(self, *args, **kwargs):
            pass

        async def health(self):
            return SimpleNamespace(version="0.5.0", healthy=False, errors=[])

    monkeypatch.setattr(server, "PhotoCraftAdapter", UnhealthyPhotoCraftAdapter)
    project = create_project(tmp_path / "preview-project")
    candidate = project.intermediates / "candidate.png"
    candidate.write_bytes(b"candidate")

    async def invoke():
        async with Client(server.create_server()) as client:
            return await client.call_tool(
                "preview",
                {
                    "project_root": str(project.root),
                    "artifact_path": str(candidate),
                },
            )

    result = asyncio.run(invoke()).structured_content

    assert result["status"] == "DEGRADED"
    assert result["errors"] == [
        {
            "engine": "photocraft",
            "stage": "health",
            "code": "ENGINE_HEALTH_CHECK_FAILED",
            "message": "Health check reported unhealthy without details",
        }
    ]


def test_inspect_engine_returns_degraded_result_when_runtime_registry_also_fails(monkeypatch) -> None:
    from ion_creative import server

    def fail_adapter(engine, project_root):
        raise TypeError("runtime registry entry malformed")

    def fail_registry():
        raise ValueError("registry invalid")

    monkeypatch.setattr(server, "_runtime_adapter", fail_adapter)
    monkeypatch.setattr(server, "load_runtime_specs", fail_registry)

    inspection = asyncio.run(server._inspect_engine("photocraft"))

    assert inspection.healthy is False
    assert inspection.version is None
    assert [error.model_dump() for error in inspection.errors] == [
        {
            "engine": "photocraft",
            "stage": "health",
            "code": "RUNTIME_INSPECTION_FAILED",
            "message": (
                "TypeError: runtime registry entry malformed; runtime registry unavailable "
                "(ValueError: registry invalid)"
            ),
        }
    ]


def test_degraded_unrelated_lightcraft_does_not_block_image_edit(tmp_path, monkeypatch) -> None:
    from mcp import Client

    from ion_creative import server

    calls = _install_fake_adapters(monkeypatch, {"lightcraft": (False, "LightCraft unavailable")})

    async def image_recipe(*args, **kwargs):
        return _fake_recipe_result()

    monkeypatch.setattr(server, "run_image_smoke", image_recipe)

    async def invoke():
        async with Client(server.create_server()) as client:
            inspection = await client.call_tool("inspect_runtime", {})
            edit = await client.call_tool(
                "edit_image",
                {
                    "project_root": str(tmp_path / "image-project"),
                    "source_path": "/tmp/disposable.png",
                    "instructions": "smoke",
                },
            )
        return inspection.structured_content, edit.structured_content

    inspection, edit = asyncio.run(invoke())

    assert next(item for item in inspection["engines"] if item["engine"] == "lightcraft")["healthy"] is False
    assert edit["status"] == "READY_FOR_REVIEW"
    assert calls.count("photocraft") == 2
    assert calls.count("lightcraft") == 1
