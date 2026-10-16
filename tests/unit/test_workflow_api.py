import asyncio
from pathlib import Path


def _invoke_workflow(arguments: dict[str, object]):
    from mcp import Client

    from ion_creative.server import create_server

    async def invoke():
        async with Client(create_server()) as client:
            return await client.call_tool("run_workflow", arguments)

    return asyncio.run(invoke()).structured_content


def test_run_workflow_rejects_unknown_workflow_without_starting_engines(tmp_path: Path) -> None:
    result = _invoke_workflow(
        {"workflow_id": "invented_workflow", "project_root": str(tmp_path / "project")}
    )

    assert result["status"] == "INVALID_WORKFLOW"
    assert result["workflow_id"] == "invented_workflow"
    assert result["errors"][0]["code"] == "UNKNOWN_WORKFLOW"


def test_run_workflow_rejects_source_path_escape_before_runtime_launch(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    outside_source = tmp_path / "outside.png"
    outside_source.write_bytes(b"not a project input")

    result = _invoke_workflow(
        {
            "workflow_id": "personal_brand_portrait",
            "project_root": str(project_root),
            "source_paths": [str(outside_source)],
        }
    )

    assert result["status"] == "FAILED"
    assert result["errors"][0]["code"] == "PROJECT_PATH_REJECTED"


def test_pdf_workflow_reports_unavailable_glyph_before_runtime_launch(tmp_path: Path) -> None:
    from ion_creative.project import create_project
    from ion_creative.workflows.pdf_ops import run

    project = create_project(tmp_path / "pdf-project")
    result = asyncio.run(run(project, brief="JEMY / Ω"))

    assert result["status"] == "UNSUPPORTED_CAPABILITY"
    assert result["errors"][0]["code"] == "UNSUPPORTED_GLYPH"
