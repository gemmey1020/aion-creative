from pathlib import Path


def _load_capabilities(project_root: Path):
    module_path = project_root / "src" / "ion_creative" / "capabilities" / "catalog.py"
    assert module_path.is_file(), "Ion live capability catalog is not implemented"
    from ion_creative.capabilities.catalog import collect_live_capabilities
    from ion_creative.capabilities.taxonomy import (
        capability_category,
        normalized_capability_id,
        summarize_argument_schema,
    )

    return (
        collect_live_capabilities,
        capability_category,
        normalized_capability_id,
        summarize_argument_schema,
    )


def test_taxonomy_normalizes_ids_and_categories() -> None:
    project_root = Path(__file__).resolve().parents[2]
    _, capability_category, normalized_capability_id, _ = _load_capabilities(project_root)

    assert normalized_capability_id("vectorcraft", "Path/Boolean Union") == (
        "vectorcraft.path_boolean_union"
    )
    assert capability_category("timeline.trim", "Trim a video clip") == "timeline/editing"
    assert capability_category("edit.exposure", "Adjust image exposure") == "color/develop"
    assert capability_category("pdf.merge", "Merge PDF pages") == "pdf"


def test_argument_schema_summary_preserves_required_names_and_types() -> None:
    project_root = Path(__file__).resolve().parents[2]
    _, _, _, summarize_argument_schema = _load_capabilities(project_root)

    summary = summarize_argument_schema(
        {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Input path"},
                "quality": {"type": "integer"},
            },
        }
    )

    assert summary["required"] == ["path"]
    assert summary["properties"]["path"] == {"type": "string", "description": "Input path"}
    assert summary["properties"]["quality"] == {"type": "integer"}


def test_catalog_uses_live_tool_discovery_and_records_source_metadata() -> None:
    import asyncio

    project_root = Path(__file__).resolve().parents[2]
    collect_live_capabilities, _, _, _ = _load_capabilities(project_root)
    from ion_creative.adapters.base import CapabilityReport, ToolDescriptor

    class FakeAdapter:
        async def discover(self):
            tool = ToolDescriptor(
                "document.open",
                "Open a project document.",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            )
            return CapabilityReport(
                engine="photocraft",
                version="0.5.0",
                status="HEALTHY",
                tool_names=[tool.name],
                tools=[tool],
                preview_tool="render_preview",
            )

    calls: list[str] = []

    def adapter_factory(engine: str, project_root: Path):
        calls.append(engine)
        return FakeAdapter()

    result = asyncio.run(
        collect_live_capabilities(engine="photocraft", adapter_factory=adapter_factory)
    )

    assert calls == ["photocraft"]
    assert result["status"] == "HEALTHY"
    assert result["engines"][0]["capability_count"] == 1
    capability = result["capabilities"][0]
    assert capability["raw_name"] == "document.open"
    assert capability["capability_id"] == "photocraft.document_open"
    assert capability["category"] == "document"
    assert capability["argument_schema_summary"]["properties"]["path"]["type"] == "string"
    assert capability["upstream_source"]["repository"] == "https://github.com/storytold/photocraft"
    assert capability["upstream_source"]["tag"] == "v0.5.0"
    assert capability["discovered_at"].endswith("+00:00")


def test_catalog_records_unavailable_adapter_as_partial_without_fake_capabilities() -> None:
    import asyncio

    project_root = Path(__file__).resolve().parents[2]
    collect_live_capabilities, _, _, _ = _load_capabilities(project_root)

    def unavailable_adapter(engine: str, project_root: Path):
        raise ValueError(f"No adapter for {engine}")

    result = asyncio.run(
        collect_live_capabilities(engine="vectorcraft", adapter_factory=unavailable_adapter)
    )

    assert result["status"] == "UNAVAILABLE"
    assert result["engines"][0]["status"] == "UNAVAILABLE"
    assert result["engines"][0]["error_code"] == "ADAPTER_UNAVAILABLE"
    assert result["capabilities"] == []


def test_catalog_marks_document_save_as_export_without_treating_state_save_as_export() -> None:
    import asyncio

    project_root = Path(__file__).resolve().parents[2]
    collect_live_capabilities, _, _, _ = _load_capabilities(project_root)
    from ion_creative.adapters.base import CapabilityReport, ToolDescriptor

    class FakeAdapter:
        async def discover(self):
            tools = [
                ToolDescriptor("doc_save", "Save the document to a file.", {"type": "object"}),
                ToolDescriptor("panel.save_state", "Save the panel state.", {"type": "object"}),
            ]
            return CapabilityReport(
                engine="photocraft",
                version="0.5.0",
                status="HEALTHY",
                tool_names=[tool.name for tool in tools],
                tools=tools,
            )

    result = asyncio.run(
        collect_live_capabilities(
            engine="photocraft", adapter_factory=lambda engine, root: FakeAdapter()
        )
    )
    by_name = {item["raw_name"]: item for item in result["capabilities"]}

    assert by_name["doc_save"]["supports_export"] is True
    assert by_name["panel.save_state"]["supports_export"] is False


def test_capabilities_cli_list_and_dump_call_live_catalog(monkeypatch, capsys) -> None:
    import json

    from ion_creative import cli

    calls: list[str | None] = []

    async def fake_collect(*, engine=None, adapter_factory=None, include_schemas=False):
        calls.append(engine)
        return {
            "status": "HEALTHY",
            "engine_filter": engine,
            "include_schemas": include_schemas,
            "capabilities": [],
        }

    monkeypatch.setattr(cli, "collect_live_capabilities", fake_collect, raising=False)

    for arguments, expected_engine in (
        (["capabilities", "list"], None),
        (["capabilities", "dump", "--engine", "photocraft"], "photocraft"),
    ):
        try:
            exit_code = cli.main(arguments)
        except SystemExit as exc:
            exit_code = exc.code
        assert exit_code == 0
        result = json.loads(capsys.readouterr().out)
        assert result["engine_filter"] == expected_engine
        assert result["include_schemas"] is (expected_engine is not None)

    assert calls == [None, "photocraft"]
