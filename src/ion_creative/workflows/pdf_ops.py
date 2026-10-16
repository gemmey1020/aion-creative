"""Inspect, preview, and prepare a project-confined PDF candidate."""

from __future__ import annotations

from ion_creative.adapters.pdfcraft import PdfCraftAdapter
from ion_creative.asset_planning import inspect_asset_file
from ion_creative.mcp.stdio_client import MCPClientError
from ion_creative.project import IonProject, ProjectSafetyError, safe_project_path
from ion_creative.recipes.image_smoke import _is_png
from ion_creative.workflows.common import (
    WorkflowFailure,
    failed,
    file_hashes,
    input_path,
    invoke_tool,
    payload,
    preview_bytes,
    project_relative,
    result,
    source_hashes_unchanged,
    write_preview,
)

WORKFLOW_ID = "pdf_ops"


def _unsupported_glyphs(text: str) -> list[str]:
    return sorted({character for character in text if ord(character) > 255})


async def run(project: IonProject, source_path: str | None = None, brief: str | None = None) -> dict[str, object]:
    source = input_path(project, source_path) if source_path else None
    text = brief.strip() if isinstance(brief, str) and brief.strip() else "JEMY / OMEGA\n\nA concise personal brand profile.\n\nCreative work, systems thinking, and practical learning."
    if source is not None and inspect_asset_file(source).get("media_type") != "pdf":
        return failed(WORKFLOW_ID, "ion", "inspect", "UNSUPPORTED_ASSET", "PDF workflow requires a PDF source document.")
    if source is None:
        if len(text) > 10_000:
            return failed(WORKFLOW_ID, "ion", "validation", "BRIEF_TOO_LONG", "PDF text brief is limited to 10,000 characters.")
        unsupported = _unsupported_glyphs(text)
        if unsupported:
            return failed(
                WORKFLOW_ID,
                "pdfcraft",
                "validation",
                "UNSUPPORTED_GLYPH",
                "PdfCraft's current default fonts cannot render: " + " ".join(unsupported),
                status="UNSUPPORTED_CAPABILITY",
            )
    before = file_hashes([source]) if source is not None else {}
    adapter = PdfCraftAdapter(project.root)
    health = await adapter.health()
    if not health.healthy:
        return failed(WORKFLOW_ID, "pdfcraft", "health", "ENGINE_UNHEALTHY", "; ".join(health.errors))
    session = None
    stages: list[dict[str, str]] = []
    try:
        session = await adapter.open_session()
        report = session.report
        if source is None:
            created = await invoke_tool(session, report, "pdfcraft", "doc_create", {"from": "text", "name": "Ion PDF Review", "text": text})
            created_payload = payload(created)
            doc_id = created_payload.get("doc") if isinstance(created_payload, dict) else None
            if not isinstance(doc_id, (int, str)):
                raise WorkflowFailure("pdfcraft", "create", "DOCUMENT_CREATE_FAILED", "PdfCraft returned no document ID.")
            stages.append({"engine": "pdfcraft", "stage": "create_from_brief", "status": "complete"})
        else:
            opened = await invoke_tool(session, report, "pdfcraft", "doc_open", {"path": project_relative(project, source)})
            opened_payload = payload(opened)
            doc_id = opened_payload.get("doc", opened_payload.get("id")) if isinstance(opened_payload, dict) else None
            if not isinstance(doc_id, (int, str)):
                raise WorkflowFailure("pdfcraft", "open", "DOCUMENT_OPEN_FAILED", "PdfCraft returned no opened document ID.")
            stages.append({"engine": "pdfcraft", "stage": "open_source_document", "status": "complete"})
        inspected = await invoke_tool(session, report, "pdfcraft", "doc_inspect", {"doc": doc_id})
        inspection = payload(inspected)
        if not isinstance(inspection, dict):
            raise WorkflowFailure("pdfcraft", "inspect", "INVALID_INSPECTION", "PdfCraft returned no document inspection details.")
        stages.append({"engine": "pdfcraft", "stage": "inspect_document", "status": "complete"})
        rendered = await invoke_tool(session, report, "pdfcraft", "render_preview", {"doc": doc_id, "page": 1, "max_side": 1200})
        preview_data = preview_bytes(rendered)
        if preview_data is None or not _is_png(preview_data):
            raise WorkflowFailure("pdfcraft", "preview", "INVALID_PREVIEW", "PdfCraft did not return a valid page preview.")
        preview_path = write_preview(project, "pdf-page-preview.png", preview_data)
        candidate = safe_project_path(project, "pdf-candidate.pdf", area="intermediates")
        saved = await invoke_tool(session, report, "pdfcraft", "doc_save", {"doc": doc_id, "path": project_relative(project, candidate)})
        if not candidate.is_file() or candidate.stat().st_size < 8 or not candidate.read_bytes().startswith(b"%PDF"):
            raise WorkflowFailure("pdfcraft", "export", "INVALID_EXPORT", "PdfCraft did not write a valid PDF candidate.")
        if source is not None and not source_hashes_unchanged([source], before):
            raise WorkflowFailure("ion", "source_integrity", "SOURCE_MUTATED", "PDF source changed during the workflow.")
        stages.extend([{"engine": "pdfcraft", "stage": "preview_first_page", "status": "complete"}, {"engine": "pdfcraft", "stage": "save_review_candidate", "status": "complete"}])
        return result(WORKFLOW_ID, status="READY_FOR_REVIEW", message="The PDF page preview and candidate are ready for review; final export remains explicit.", engines=["pdfcraft"], exact_versions={"pdfcraft": health.version}, output_paths=[str(candidate)], preview_paths=[str(preview_path)], stages=stages, source_hashes=before, details={"document": inspection, "save_result": payload(saved), "format": "pdf"})
    except (WorkflowFailure, MCPClientError, OSError, ProjectSafetyError, ValueError, TypeError) as exc:
        from ion_creative.workflows.common import failure_from_exception

        return failure_from_exception(WORKFLOW_ID, exc, fallback_engine="pdfcraft")
    finally:
        if session is not None:
            await session.close()
