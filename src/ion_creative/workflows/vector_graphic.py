"""Create a small reviewable vector brand card using VectorCraft."""

from __future__ import annotations

from ion_creative.adapters.base import PreviewRequest
from ion_creative.adapters.vectorcraft import VectorCraftAdapter
from ion_creative.mcp.stdio_client import MCPClientError
from ion_creative.project import IonProject, ProjectSafetyError, safe_project_path
from ion_creative.recipes.image_smoke import _is_png
from ion_creative.workflows.common import (
    WorkflowFailure,
    execute_live_command,
    failed,
    invoke_tool,
    payload,
    preview_bytes,
    project_relative,
    result,
    write_preview,
)

WORKFLOW_ID = "vector_graphic"


async def run(project: IonProject, brief: str | None = None) -> dict[str, object]:
    title = brief.strip() if isinstance(brief, str) and brief.strip() else "JEMY / Ω"
    adapter = VectorCraftAdapter(project.root)
    health = await adapter.health()
    if not health.healthy:
        return failed(WORKFLOW_ID, "vectorcraft", "health", "ENGINE_UNHEALTHY", "; ".join(health.errors))
    session = None
    stages: list[dict[str, str]] = []
    try:
        session = await adapter.open_session()
        report = session.report
        for tool in ("command_run", "command_list", "draw_shape", "add_text", "render_preview", "export"):
            if report.tool_bindings.get(tool, tool) not in report.tool_names:
                raise WorkflowFailure("vectorcraft", "discovery", "UNSUPPORTED_CAPABILITY", f"VectorCraft does not expose `{tool}`.")
        await execute_live_command(session, report, "vectorcraft", "file.new", {"name": "Ion Vector Brand Card", "width": 720, "height": 900, "units": "Pixels", "orientation": "portrait", "backgroundContents": "white", "colorMode": "rgb"})
        stages.append({"engine": "vectorcraft", "stage": "create_artboard", "status": "complete"})
        await invoke_tool(session, report, "vectorcraft", "draw_shape", {"shape": "rectangle", "x": 0, "y": 0, "width": 720, "height": 900, "fill": "#10151d", "stroke": "none"})
        await invoke_tool(session, report, "vectorcraft", "draw_shape", {"shape": "ellipse", "x": 548, "y": 84, "width": 72, "height": 72, "fill": "#d6b36b", "stroke": "none"})
        await invoke_tool(session, report, "vectorcraft", "add_text", {"text": "Ω", "x": 565, "y": 139, "size": 46, "font": "Sans", "color": "#10151d"})
        await invoke_tool(session, report, "vectorcraft", "add_text", {"text": title[:72], "x": 72, "y": 470, "size": 48, "font": "Sans", "color": "#f4f1eb"})
        await invoke_tool(session, report, "vectorcraft", "add_text", {"text": "CREATIVE SYSTEMS / PERSONAL BRAND", "x": 74, "y": 525, "size": 16, "font": "Sans", "color": "#d6b36b"})
        stages.append({"engine": "vectorcraft", "stage": "compose_vector_card", "status": "complete"})
        rendered = await session.preview(PreviewRequest(arguments={"max_side": 1000}))
        preview_data = preview_bytes(rendered) if rendered.ok else None
        if preview_data is None or not _is_png(preview_data):
            raise WorkflowFailure("vectorcraft", "preview", "INVALID_PREVIEW", "VectorCraft did not return a valid PNG preview.")
        preview_path = write_preview(project, "vector-card-preview.png", preview_data)
        candidate = safe_project_path(project, "vector-brand-card.svg", area="intermediates")
        exported = await invoke_tool(session, report, "vectorcraft", "export", {"path": project_relative(project, candidate), "format": "svg"})
        if not candidate.is_file() or candidate.stat().st_size == 0 or b"<svg" not in candidate.read_bytes()[:2048].lower():
            raise WorkflowFailure("vectorcraft", "export", "INVALID_EXPORT", "VectorCraft did not write a valid SVG candidate.")
        stages.append({"engine": "vectorcraft", "stage": "export_review_candidate", "status": "complete"})
        return result(WORKFLOW_ID, status="READY_FOR_REVIEW", message="Vector brand-card preview and SVG candidate are ready for visual review.", engines=["vectorcraft"], exact_versions={"vectorcraft": health.version}, output_paths=[str(candidate)], preview_paths=[str(preview_path)], stages=stages, details={"format": "svg", "export_result": payload(exported)})
    except (WorkflowFailure, MCPClientError, OSError, ProjectSafetyError, ValueError, TypeError) as exc:
        from ion_creative.workflows.common import failure_from_exception

        return failure_from_exception(WORKFLOW_ID, exc, fallback_engine="vectorcraft")
    finally:
        if session is not None:
            await session.close()
