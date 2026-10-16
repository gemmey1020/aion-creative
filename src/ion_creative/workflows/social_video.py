"""Preview-first social-video assembly using live FilmCraft capabilities."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ion_creative.adapters.base import ExecuteRequest, PreviewRequest
from ion_creative.adapters.filmcraft import FilmCraftAdapter
from ion_creative.asset_planning import inspect_asset_file
from ion_creative.mcp.stdio_client import MCPClientError
from ion_creative.project import IonProject, ProjectSafetyError, safe_project_path
from ion_creative.recipes.image_smoke import _is_png
from ion_creative.recipes.video_smoke import _valid_mp4
from ion_creative.workflows.common import (
    WorkflowFailure,
    execute_live_command,
    failed,
    file_hashes,
    input_path,
    invoke_tool,
    payload,
    preview_bytes,
    result,
    source_hashes_unchanged,
    write_preview,
)

WORKFLOW_ID = "social_video"


def _video_clips(sequence: object) -> list[dict[str, object]]:
    if not isinstance(sequence, dict):
        return []
    tracks = sequence.get("video")
    if not isinstance(tracks, list):
        return []
    clips: list[dict[str, object]] = []
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("items"), list):
            continue
        clips.extend(item for item in track["items"] if isinstance(item, dict))
    return clips


async def run(project: IonProject, source_paths: list[str]) -> dict[str, object]:
    if len(source_paths) < 2:
        return failed(WORKFLOW_ID, "ion", "validation", "TWO_SOURCES_REQUIRED", "Social video requires at least two clips.")
    if len(source_paths) > 10:
        return failed(WORKFLOW_ID, "ion", "validation", "SOURCE_LIMIT_EXCEEDED", "Social video accepts at most ten clips per run.")
    sources = [input_path(project, value) for value in source_paths]
    inspections = [inspect_asset_file(source) for source in sources]
    if any(item.get("media_type") != "video" for item in inspections):
        return failed(WORKFLOW_ID, "ion", "inspect", "UNSUPPORTED_ASSET", "Every social-video source must be a video.")
    before = file_hashes(sources)
    adapter = FilmCraftAdapter(demo=True, project_root=project.root)
    health = await adapter.health()
    if not health.healthy:
        return failed(WORKFLOW_ID, "filmcraft", "health", "ENGINE_UNHEALTHY", "; ".join(health.errors))
    session = None
    stages: list[dict[str, str]] = []
    try:
        session = await adapter.open_session()
        report = session.report
        for name in ("file.import", "file.newSequence", "file.exportMedia"):
            await _require_live(session, report, name)
        imported_result = await execute_live_command(
            session,
            report,
            "filmcraft",
            "file.import",
            {"paths": [str(source) for source in sources]},
        )
        imported_payload = payload(imported_result)
        imported_ids = imported_payload.get("items", []) if isinstance(imported_payload, dict) else []
        if not isinstance(imported_ids, list) or len(imported_ids) < 2:
            raise WorkflowFailure("filmcraft", "import", "IMPORT_FAILED", "FilmCraft did not return two imported media IDs.")
        stages.append({"engine": "filmcraft", "stage": "import_sources", "status": "complete"})

        sequence_result = await execute_live_command(
            session,
            report,
            "filmcraft",
            "file.newSequence",
            {"name": "Ion Social Video", "width": 1080, "height": 1920, "fps": 30, "fromItems": imported_ids},
        )
        sequence_payload = payload(sequence_result)
        sequence_id = sequence_payload.get("sequence") if isinstance(sequence_payload, dict) else None
        if not isinstance(sequence_id, (int, str)):
            raise WorkflowFailure("filmcraft", "sequence", "SEQUENCE_CREATE_FAILED", "FilmCraft returned no sequence ID.")
        stages.append({"engine": "filmcraft", "stage": "create_vertical_sequence", "status": "complete"})

        inspected_sequence = await invoke_tool(session, report, "filmcraft", "sequence_inspect", {})
        sequence_data = payload(inspected_sequence)
        clips = _video_clips(sequence_data)
        if len(clips) < 2:
            raise WorkflowFailure("filmcraft", "inspect", "SEQUENCE_INCOMPLETE", "FilmCraft sequence inspection did not show both imported clips.")
        stages.append({"engine": "filmcraft", "stage": "inspect_sequence", "status": "complete"})

        # A one-frame trim is bounded and comes from FilmCraft's live command catalog.
        trim_id = await _find_trim_command(session, report)
        first_clip = clips[0].get("clip")
        if isinstance(first_clip, (int, str)):
            trim_result = await session.execute(
                ExecuteRequest(arguments={"id": trim_id, "params": {"clip": first_clip, "edge": "out", "mode": "regular", "deltaFrames": -1}})
            )
            if not trim_result.ok:
                raise WorkflowFailure("filmcraft", "timeline_edit", trim_result.error_code or "TIMELINE_EDIT_FAILED", trim_result.message)
            stages.append({"engine": "filmcraft", "stage": "trim_first_clip_by_one_frame", "status": "complete"})

        rendered = await session.preview(PreviewRequest(arguments={"seconds": 0.5, "max_side": 960}))
        preview_data = preview_bytes(rendered) if rendered.ok else None
        if preview_data is None or not _is_png(preview_data):
            raise WorkflowFailure("filmcraft", "preview", "INVALID_PREVIEW", "FilmCraft did not return a valid sequence preview.")
        preview_path = write_preview(project, "social-video-preview.png", preview_data)
        stages.append({"engine": "filmcraft", "stage": "preview_sequence", "status": "complete"})

        candidate = safe_project_path(project, "social-video-candidate.mp4", area="intermediates")
        export = await execute_live_command(
            session,
            report,
            "filmcraft",
            "file.exportMedia",
            {"path": str(candidate), "format": "h264", "range": "entire", "width": 1080, "height": 1920, "fps": 30.0, "audio": False, "wait": True},
        )
        if not candidate.is_file() or not _valid_mp4(candidate):
            raise WorkflowFailure("filmcraft", "export", "INVALID_MP4", "FilmCraft did not write a valid H.264 MP4 candidate.")
        if not source_hashes_unchanged(sources, before):
            raise WorkflowFailure("ion", "source_integrity", "SOURCE_MUTATED", "A source video changed during the workflow.")
        stages.append({"engine": "filmcraft", "stage": "export_review_candidate", "status": "complete"})
        _write_social_video_report(project, candidate, preview_path, health.version)
        return result(
            WORKFLOW_ID,
            status="READY_FOR_REVIEW",
            message="Vertical social-video candidate and preview are ready; final export remains an explicit step.",
            engines=["filmcraft"],
            exact_versions={"filmcraft": health.version},
            output_paths=[str(candidate)],
            preview_paths=[str(preview_path)],
            stages=stages,
            source_hashes=before,
            details={"sequence_id": sequence_id, "clip_count": len(clips), "dimensions": "1080x1920", "format": "h264", "export_result": payload(export)},
        )
    except (WorkflowFailure, MCPClientError, OSError, ProjectSafetyError, ValueError, TypeError) as exc:
        from ion_creative.workflows.common import failure_from_exception

        return failure_from_exception(WORKFLOW_ID, exc, fallback_engine="filmcraft", fallback_stage="workflow")
    finally:
        if session is not None:
            await session.close()


def _write_social_video_report(
    project: IonProject, candidate: Path, preview: Path, version: str | None
) -> None:
    if project.ion.is_symlink() or not project.ion.is_dir():
        raise ProjectSafetyError("Ion metadata area must be a real directory")
    report_path = project.ion / "social-video-report.json"
    if report_path.is_symlink() or (report_path.exists() and not report_path.is_file()):
        raise ProjectSafetyError("Social-video report path must be a regular file")
    if report_path.exists():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProjectSafetyError("Existing social-video report is not valid Ion metadata") from exc
        if not isinstance(previous, dict) or previous.get("recipe") != WORKFLOW_ID:
            raise ProjectSafetyError("Existing social-video report is not owned by this workflow")
    report = {
        "recipe": WORKFLOW_ID,
        "status": "READY_FOR_REVIEW",
        "candidate_paths": [str(candidate)],
        "preview_paths": [str(preview)],
        "engines_used": ["filmcraft"],
        "exact_versions": {"filmcraft": version} if version else {},
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=project.ion,
            prefix=".social-video-report.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        os.replace(temporary_path, report_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def _require_live(session, report, command_id: str) -> None:
    from ion_creative.workflows.common import live_command_id

    await live_command_id(session, report, "filmcraft", command_id)


async def _find_trim_command(session, report) -> str:
    from ion_creative.recipes.image_smoke import _response_object
    from ion_creative.workflows.common import ensure_tool

    command_tool = ensure_tool(report, "command_list", "filmcraft")
    listed = await session.invoke_tool(command_tool, {"filter": "trim", "enabled_only": True})
    if not listed.ok:
        raise WorkflowFailure("filmcraft", "command_discovery", listed.error_code or "COMMAND_DISCOVERY_FAILED", listed.message)
    data = _response_object(listed.result)
    entries = data.get("commands", data.get("result", [])) if isinstance(data, dict) else data
    matches = [item for item in entries if isinstance(item, dict) and item.get("enabled") is True and str(item.get("label", "")).strip().casefold() == "trim edit" and isinstance(item.get("id"), str) and '"clip"' in str(item.get("params", "")) and '"deltaFrames"' in str(item.get("params", ""))] if isinstance(entries, list) else []
    if len(matches) != 1:
        raise WorkflowFailure("filmcraft", "command_discovery", "UNSUPPORTED_CAPABILITY", "FilmCraft exposed no unambiguous live trim command.")
    return str(matches[0]["id"])
