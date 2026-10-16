import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

REQUIRED_TOOLS = {
    "command_list",
    "command_run",
    "command_batch",
    "doc_inspect",
    "render_preview",
}


def _load_adapters(project_root: Path):
    base_path = project_root / "src" / "ion_creative" / "adapters" / "base.py"
    assert base_path.is_file(), "Craft capability adapters are not implemented"
    from ion_creative.adapters.base import (
        BatchRequest,
        ExecuteRequest,
        InspectRequest,
        PreviewRequest,
    )
    from ion_creative.adapters.effectcraft import EffectCraftAdapter
    from ion_creative.adapters.filmcraft import FilmCraftAdapter
    from ion_creative.adapters.lightcraft import LightCraftAdapter
    from ion_creative.adapters.photocraft import PhotoCraftAdapter

    return (
        BatchRequest,
        EffectCraftAdapter,
        ExecuteRequest,
        FilmCraftAdapter,
        InspectRequest,
        LightCraftAdapter,
        PhotoCraftAdapter,
        PreviewRequest,
    )


class FakeClient:
    initialized = True

    def __init__(self, names: set[str]):
        self.names = names
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self):
        tools = [
            SimpleNamespace(
                name=name,
                description=f"description for {name}",
                input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
            )
            for name in sorted(self.names)
        ]
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, content=[SimpleNamespace(text="ok")])


def _mock_client_factory(monkeypatch, module, clients: dict[str, FakeClient]) -> None:
    @asynccontextmanager
    async def fake_open(command: Path, _args: list[str], cwd: Path | None = None, **_kwargs):
        engine = command.name.removesuffix("-cli")
        yield clients[engine]

    monkeypatch.setattr(module, "open_stdio_mcp", fake_open)


def test_adapter_discovery_records_live_tools_and_schemas(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (
        _,
        _,
        _,
        _,
        _,
        _,
        PhotoCraftAdapter,
        _,
    ) = _load_adapters(project_root)
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    names = REQUIRED_TOOLS | {"undocumented_extra_tool"}
    client = FakeClient(names)
    from ion_creative.adapters import base

    _mock_client_factory(monkeypatch, base, {"photocraft": client})
    adapter = PhotoCraftAdapter(project_root=tmp_path)

    report = asyncio.run(adapter.discover())

    assert report.status == "HEALTHY"
    assert report.tool_names == sorted(names)
    assert report.missing_tools == []
    assert report.tool_schemas["command_run"] == {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    assert report.recipe_availability["image_edit"] is True


def test_live_tool_schema_rejects_wrong_argument_types_before_call(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (_, _, _, _, _, _, PhotoCraftAdapter, _) = _load_adapters(project_root)
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    client = FakeClient(REQUIRED_TOOLS)
    from ion_creative.adapters import base

    _mock_client_factory(monkeypatch, base, {"photocraft": client})
    adapter = PhotoCraftAdapter(project_root=tmp_path)

    result = asyncio.run(adapter.invoke_tool("command_run", {"value": 42}))

    assert result.ok is False
    assert result.error_code == "INVALID_ARGUMENTS"
    assert "value" in result.message
    assert client.calls == []


def test_adapter_health_combines_runtime_doctor_with_live_mcp_report(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (_, _, _, _, _, _, PhotoCraftAdapter, _) = _load_adapters(project_root)
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    from ion_creative.adapters import base

    _mock_client_factory(monkeypatch, base, {"photocraft": FakeClient(REQUIRED_TOOLS)})
    monkeypatch.setattr(
        base,
        "doctor_engine",
        lambda _engine: SimpleNamespace(healthy=True, message="Pinned CLI version verified"),
    )

    health = asyncio.run(PhotoCraftAdapter(tmp_path).health())

    assert health.healthy is True
    assert health.mcp_initialized is True
    assert health.capability_report is not None
    assert health.capability_report.status == "HEALTHY"


def test_documented_preview_equivalents_are_normalized(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (
        _,
        EffectCraftAdapter,
        _,
        _,
        _,
        _,
        PhotoCraftAdapter,
        _,
    ) = _load_adapters(project_root)
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    photo_tools = (REQUIRED_TOOLS - {"render_preview"}) | {"doc_render_preview"}
    effect_tools = {
        "list_commands",
        "execute_command",
        "batch",
        "get_project",
        "render_frame",
    }
    from ion_creative.adapters import base

    _mock_client_factory(
        monkeypatch,
        base,
        {"photocraft": FakeClient(photo_tools), "effectcraft": FakeClient(effect_tools)},
    )

    photo_report, effect_report = asyncio.run(
        _discover_both(
            PhotoCraftAdapter(tmp_path),
            EffectCraftAdapter(),
        )
    )

    assert photo_report.status == "HEALTHY"
    assert photo_report.preview_tool == "doc_render_preview"
    assert effect_report.status == "HEALTHY"
    assert effect_report.preview_tool == "render_frame"
    assert effect_report.tool_bindings == {
        "command_list": "list_commands",
        "command_run": "execute_command",
        "command_batch": "batch",
        "doc_inspect": "get_project",
        "render_preview": "render_frame",
    }


def test_missing_tool_degrades_dependent_recipe_without_affecting_other_engine(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (
        _,
        _,
        _,
        FilmCraftAdapter,
        _,
        _,
        PhotoCraftAdapter,
        _,
    ) = _load_adapters(project_root)
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    photo = FakeClient(REQUIRED_TOOLS - {"command_run"})
    film = FakeClient(REQUIRED_TOOLS)
    from ion_creative.adapters import base

    _mock_client_factory(monkeypatch, base, {"photocraft": photo, "filmcraft": film})

    photo_report, film_report = asyncio.run(
        _discover_both(PhotoCraftAdapter(tmp_path), FilmCraftAdapter())
    )

    assert photo_report.status == "DEGRADED"
    assert photo_report.missing_tools == ["command_run"]
    assert photo_report.recipe_availability["image_edit"] is False
    assert film_report.status == "HEALTHY"
    assert film_report.recipe_availability["video_edit"] is True


async def _discover_both(photo, film):
    return await photo.discover(), await film.discover()


def test_adapter_launch_commands_match_runtime_and_project_scope(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (
        _,
        EffectCraftAdapter,
        _,
        FilmCraftAdapter,
        _,
        LightCraftAdapter,
        PhotoCraftAdapter,
        _,
    ) = _load_adapters(project_root)
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    project = tmp_path / "ion-project"
    project.mkdir()

    photo = PhotoCraftAdapter(project)
    film = FilmCraftAdapter()
    effect = EffectCraftAdapter()
    light = LightCraftAdapter(demo=True)

    assert photo.command == runtime_root / "photocraft" / "0.5.0" / "bin" / "photocraft-cli"
    assert photo.args == [
        "mcp",
        "--automation-read-root",
        str(project.resolve()),
        "--automation-write-root",
        str(project.resolve()),
    ]
    assert film.args == ["mcp"]
    assert effect.args == ["mcp"]
    assert light.args == ["mcp", "--demo"]


def test_demo_adapters_keep_application_state_inside_the_ion_project(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    _, EffectCraftAdapter, _, FilmCraftAdapter, _, _, _, _ = _load_adapters(project_root)
    project = tmp_path / "video-project"
    project.mkdir()

    film = FilmCraftAdapter(demo=True, project_root=project)
    effect = EffectCraftAdapter(demo=True, project_root=project)

    assert film.args == ["mcp", "--demo"]
    assert effect.args == ["mcp", "--demo"]
    assert film.cwd == project.resolve()
    assert effect.cwd == project.resolve()
    assert film.env_overrides["HOME"] == str(project / "ion" / "filmcraft-home")
    assert effect.env_overrides["HOME"] == str(project / "ion" / "effectcraft-home")
    assert Path(film.env_overrides["HOME"]).is_dir()
    assert Path(effect.env_overrides["HOME"]).is_dir()


def test_adapter_operations_use_only_declared_high_level_tools(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (
        BatchRequest,
        _,
        ExecuteRequest,
        _,
        InspectRequest,
        _,
        PhotoCraftAdapter,
        PreviewRequest,
    ) = _load_adapters(project_root)
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    client = FakeClient(REQUIRED_TOOLS)
    from ion_creative.adapters import base

    _mock_client_factory(monkeypatch, base, {"photocraft": client})
    adapter = PhotoCraftAdapter(project_root=tmp_path)

    async def execute_operations():
        results = [
            await adapter.inspect(InspectRequest(arguments={"path": "input.png"})),
            await adapter.execute(ExecuteRequest(arguments={"command": "opaque-id"})),
            await adapter.batch(BatchRequest(arguments={"commands": []})),
            await adapter.preview(PreviewRequest(arguments={"path": "result.png"})),
        ]
        return results

    results = asyncio.run(execute_operations())

    assert all(result.ok for result in results)
    assert [name for name, _ in client.calls] == [
        "doc_inspect",
        "command_run",
        "command_batch",
        "render_preview",
    ]


def test_effectcraft_operations_translate_verified_upstream_aliases(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (
        BatchRequest,
        EffectCraftAdapter,
        ExecuteRequest,
        _,
        _,
        _,
        _,
        _,
    ) = _load_adapters(project_root)
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    names = {"list_commands", "execute_command", "batch", "get_project", "render_frame"}
    client = FakeClient(names)
    from ion_creative.adapters import base

    _mock_client_factory(monkeypatch, base, {"effectcraft": client})
    adapter = EffectCraftAdapter()

    async def execute_operations():
        execute_result = await adapter.execute(
            ExecuteRequest(arguments={"id": "comp.new", "params": {"name": "Smoke"}})
        )
        batch_result = await adapter.batch(
            BatchRequest(
                arguments={
                    "steps": [{"id": "layer.newText", "params": {"text": "Smoke"}}],
                    "stop_on_error": True,
                }
            )
        )
        unsupported = await adapter.batch(
            BatchRequest(arguments={"steps": [], "stop_on_error": False})
        )
        return execute_result, batch_result, unsupported

    result, batch_result, unsupported = asyncio.run(execute_operations())

    assert result.ok is True
    assert batch_result.ok is True
    assert unsupported.error_code == "UNSUPPORTED_ARGUMENTS"
    assert client.calls == [
        ("execute_command", {"command": "comp.new", "params": {"name": "Smoke"}}),
        (
            "batch",
            {
                "steps": [{"command": "layer.newText", "params": {"text": "Smoke"}}],
                "atomic": False,
            },
        ),
    ]


def test_new_craft_adapters_initialize_live_tools_with_isolated_mcp_modes(
    tmp_path: Path, monkeypatch
) -> None:
    from ion_creative.adapters import base
    from ion_creative.adapters.designcraft import DesignCraftAdapter
    from ion_creative.adapters.pdfcraft import PdfCraftAdapter
    from ion_creative.adapters.vectorcraft import VectorCraftAdapter

    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    project = tmp_path / "new-craft-project"
    project.mkdir()
    clients = {
        "vectorcraft": FakeClient(REQUIRED_TOOLS),
        "designcraft": FakeClient(
            {"list_commands", "execute", "batch", "inspect_document", "render_page"}
        ),
        "pdfcraft": FakeClient(REQUIRED_TOOLS),
    }
    _mock_client_factory(monkeypatch, base, clients)

    vector = VectorCraftAdapter(project)
    design = DesignCraftAdapter(project)
    pdf = PdfCraftAdapter(project)

    async def discover_all():
        return await asyncio.gather(vector.discover(), design.discover(), pdf.discover())

    reports = asyncio.run(discover_all())

    assert vector.args == [
        "mcp",
        "--headless",
        "--automation-read-root",
        str(project.resolve() / "inputs"),
        "--automation-write-root",
        str(project.resolve()),
    ]
    assert (project / "inputs").is_dir()
    assert design.args == ["mcp", "--sample"]
    assert pdf.args == ["mcp", "--root", str(project.resolve())]
    assert all(report.status == "HEALTHY" for report in reports)
    assert reports[1].tool_bindings == {
        "command_list": "list_commands",
        "command_run": "execute",
        "command_batch": "batch",
        "doc_inspect": "inspect_document",
        "render_preview": "render_page",
    }


def test_designcraft_normalized_execute_and_batch_translate_live_aliases(
    tmp_path: Path, monkeypatch
) -> None:
    from ion_creative.adapters import base
    from ion_creative.adapters.base import BatchRequest, ExecuteRequest
    from ion_creative.adapters.designcraft import DesignCraftAdapter

    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    client = FakeClient({"list_commands", "execute", "batch", "inspect_document", "render_page"})
    _mock_client_factory(monkeypatch, base, {"designcraft": client})
    adapter = DesignCraftAdapter(tmp_path)

    async def execute_operations():
        execute = await adapter.execute(
            ExecuteRequest(arguments={"id": "frame.create", "params": {"rect": [0, 0, 80, 80]}})
        )
        batch = await adapter.batch(
            BatchRequest(
                arguments={
                    "steps": [{"id": "frame.create", "params": {"rect": [0, 0, 80, 80]}}],
                    "stop_on_error": True,
                }
            )
        )
        return execute, batch

    execute, batch = asyncio.run(execute_operations())

    assert execute.ok is True
    assert batch.ok is True
    assert client.calls == [
        ("execute", {"command": "frame.create", "params": {"rect": [0, 0, 80, 80]}}),
        (
            "batch",
            {"commands": [{"command": "frame.create", "params": {"rect": [0, 0, 80, 80]}}]},
        ),
    ]


def test_new_adapters_route_inspect_execute_batch_preview_and_live_export_tools(
    tmp_path: Path, monkeypatch
) -> None:
    from ion_creative.adapters import base
    from ion_creative.adapters.base import (
        BatchRequest,
        ExecuteRequest,
        InspectRequest,
        PreviewRequest,
    )
    from ion_creative.adapters.designcraft import DesignCraftAdapter
    from ion_creative.adapters.pdfcraft import PdfCraftAdapter
    from ion_creative.adapters.vectorcraft import VectorCraftAdapter

    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    names = {
        "vectorcraft": REQUIRED_TOOLS | {"export"},
        "designcraft": {"list_commands", "execute", "batch", "inspect_document", "render_page", "export_png"},
        "pdfcraft": REQUIRED_TOOLS | {"doc_export_text"},
    }
    clients = {engine: FakeClient(tools) for engine, tools in names.items()}
    _mock_client_factory(monkeypatch, base, clients)
    adapters = {
        "vectorcraft": (VectorCraftAdapter(tmp_path / "vector"), "export"),
        "designcraft": (DesignCraftAdapter(tmp_path / "design"), "export_png"),
        "pdfcraft": (PdfCraftAdapter(tmp_path / "pdf"), "doc_export_text"),
    }

    async def exercise():
        results = {}
        for engine, (adapter, export_tool) in adapters.items():
            results[engine] = [
                await adapter.inspect(InspectRequest(arguments={})),
                await adapter.execute(ExecuteRequest(arguments={"id": "command.one", "params": {}})),
                await adapter.batch(BatchRequest(arguments={"steps": []})),
                await adapter.preview(PreviewRequest(arguments={})),
                await adapter.invoke_tool(export_tool, {}),
            ]
        return results

    results = asyncio.run(exercise())

    assert all(all(result.ok for result in group) for group in results.values())
    assert [name for name, _ in clients["vectorcraft"].calls] == [
        "doc_inspect",
        "command_run",
        "command_batch",
        "render_preview",
        "export",
    ]
    assert [name for name, _ in clients["designcraft"].calls] == [
        "inspect_document",
        "execute",
        "batch",
        "render_page",
        "export_png",
    ]
    assert [name for name, _ in clients["pdfcraft"].calls] == [
        "doc_inspect",
        "command_run",
        "command_batch",
        "render_preview",
        "doc_export_text",
    ]
