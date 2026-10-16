"""Create a compact editorial layout using DesignCraft's live command catalog."""

from __future__ import annotations

from ion_creative.adapters.designcraft import DesignCraftAdapter
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
)

WORKFLOW_ID = "design_layout"


async def run(project: IonProject, brief: str | None = None) -> dict[str, object]:
    title = brief.strip() if isinstance(brief, str) and brief.strip() else "Ideas into visible work"
    adapter = DesignCraftAdapter(project.root)
    health = await adapter.health()
    if not health.healthy:
        return failed(WORKFLOW_ID, "designcraft", "health", "ENGINE_UNHEALTHY", "; ".join(health.errors))
    session = None
    stages: list[dict[str, str]] = []
    try:
        session = await adapter.open_session()
        report = session.report
        for tool in ("new_document", "execute", "list_commands", "render_page"):
            if report.tool_bindings.get(tool, tool) not in report.tool_names:
                raise WorkflowFailure("designcraft", "discovery", "UNSUPPORTED_CAPABILITY", f"DesignCraft does not expose `{tool}`.")
        await invoke_tool(session, report, "designcraft", "new_document", {"title": "Ion Editorial Layout", "width": 432, "height": 540, "pages": 1, "columns": 1, "margins": 28, "sample": False})
        stages.append({"engine": "designcraft", "stage": "create_page", "status": "complete"})
        await execute_live_command(session, report, "designcraft", "frame.create", {"rect": [40, 50, 392, 132], "shape": "rectangle", "content": "text", "text": "JEMY / Ω", "caret": False})
        await execute_live_command(session, report, "designcraft", "frame.create", {"rect": [40, 170, 390, 340], "shape": "rectangle", "content": "text", "text": title[:180], "caret": False})
        await execute_live_command(session, report, "designcraft", "frame.create", {"rect": [40, 450, 392, 490], "shape": "rectangle", "content": "text", "text": "BUILD  •  LEARN  •  SHARE", "caret": False})
        stages.append({"engine": "designcraft", "stage": "arrange_type_frames", "status": "complete"})
        preview_path = safe_project_path(project, "design-layout-preview.png", area="previews")
        rendered = await invoke_tool(session, report, "designcraft", "render_page", {"page": 0, "path": str(preview_path), "scale": 1.5})
        if not preview_path.is_file() or not _is_png(preview_path.read_bytes()):
            inline = preview_bytes(rendered)
            if inline is None or not _is_png(inline):
                raise WorkflowFailure("designcraft", "preview", "INVALID_PREVIEW", "DesignCraft did not render a valid page preview.")
            preview_path.write_bytes(inline)
        candidate = safe_project_path(project, "design-layout-candidate.pdf", area="intermediates")
        exported = await execute_live_command(session, report, "designcraft", "file.exportPdf", {"path": project_relative(project, candidate), "pages": [1]})
        if not candidate.is_file() or candidate.stat().st_size < 8 or not candidate.read_bytes().startswith(b"%PDF"):
            raise WorkflowFailure("designcraft", "export", "INVALID_EXPORT", "DesignCraft did not write a valid PDF candidate.")
        stages.extend([{"engine": "designcraft", "stage": "preview_page", "status": "complete"}, {"engine": "designcraft", "stage": "export_review_candidate", "status": "complete"}])
        return result(WORKFLOW_ID, status="READY_FOR_REVIEW", message="The editorial page preview and PDF candidate are ready for visual review.", engines=["designcraft"], exact_versions={"designcraft": health.version}, output_paths=[str(candidate)], preview_paths=[str(preview_path)], stages=stages, details={"pages": 1, "format": "pdf", "export_result": payload(exported)})
    except (WorkflowFailure, MCPClientError, OSError, ProjectSafetyError, ValueError, TypeError) as exc:
        from ion_creative.workflows.common import failure_from_exception

        return failure_from_exception(WORKFLOW_ID, exc, fallback_engine="designcraft")
    finally:
        if session is not None:
            await session.close()
