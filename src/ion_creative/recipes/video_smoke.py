"""Safe FilmCraft demo workflow with a best-effort EffectCraft frame proof."""

import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from ion_creative.adapters.base import ExecuteRequest, PreviewRequest
from ion_creative.adapters.effectcraft import EffectCraftAdapter
from ion_creative.adapters.filmcraft import FilmCraftAdapter
from ion_creative.mcp.stdio_client import MCPClientError
from ion_creative.project import IonProject, ProjectSafetyError
from ion_creative.recipes.image_smoke import (
    RecipeResult,
    _is_png,
    _preview_png,
    _response_object,
    _unique_output,
)
from ion_creative.runtime.registry import load_runtime_specs

MP4_FTYP = b"ftyp"


def _valid_mp4(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 24:
        return False
    with path.open("rb") as stream:
        stream.seek(4)
        return stream.read(4) == MP4_FTYP


def _json_payload(value: object | None) -> object | None:
    payload = _response_object(value)
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload


def _choose_trim_command(payload: object) -> dict[str, object] | None:
    if isinstance(payload, dict):
        payload = payload.get("commands", payload.get("result", []))
    if not isinstance(payload, list):
        return None
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("enabled") is True
        and str(item.get("label", "")).strip().casefold() == "trim edit"
        and all(token in str(item.get("params", "")) for token in ('"clip"', '"deltaFrames"'))
        and isinstance(item.get("id"), str)
    ]
    return matches[0] if len(matches) == 1 else None


def _choose_h264_export(payload: object) -> dict[str, object] | None:
    if isinstance(payload, dict):
        payload = payload.get("commands", payload.get("result", []))
    if not isinstance(payload, list):
        return None
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("enabled") is True
        and isinstance(item.get("id"), str)
        and isinstance(item.get("params"), str)
        and '"path"' in item["params"]
        and "h264|" in item["params"]
    ]
    return matches[0] if len(matches) == 1 else None


def _first_video_clip(sequence: object) -> int | str | None:
    if not isinstance(sequence, dict):
        return None
    tracks = sequence.get("video")
    if not isinstance(tracks, list):
        return None
    for track in tracks:
        if not isinstance(track, dict):
            continue
        items = track.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("clip"), (int, str)):
                return item["clip"]
    return None


async def run_video_smoke(
    project: IonProject,
    *,
    film_adapter=None,
    effect_adapter=None,
) -> RecipeResult:
    """Trim a disposable FilmCraft demo, preview it, and return an MP4 review candidate."""
    specs = load_runtime_specs()
    exact_versions = {name: spec.version for name, spec in specs.items()}
    film = film_adapter or FilmCraftAdapter(demo=True, project_root=project.root)
    effect = effect_adapter or EffectCraftAdapter(demo=True, project_root=project.root)
    engines_used: list[str] = []
    capabilities: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    stages: list[dict[str, str]] = []
    candidate_paths: list[str] = []
    preview_paths: list[str] = []
    report_path = project.ion / "video-smoke-report.json"
    film_session = None
    effect_session = None

    def finish(status: str) -> RecipeResult:
        result = RecipeResult(
            recipe="video_smoke",
            status=status,
            project_root=str(project.root),
            engines_used=list(dict.fromkeys(engines_used)),
            exact_versions=exact_versions,
            capabilities=capabilities,
            candidate_paths=candidate_paths,
            preview_paths=preview_paths,
            errors=errors,
            stages=stages,
        )
        if project.ion.is_symlink() or not project.ion.is_dir() or report_path.is_symlink():
            raise ProjectSafetyError("Ion video report directory or report path is not safe")
        temporary = project.ion / f".video-smoke-{uuid.uuid4().hex}.tmp"
        payload = replace(result, report_path=str(report_path)).to_dict()
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, report_path)
        finally:
            temporary.unlink(missing_ok=True)
        return replace(result, report_path=str(report_path))

    try:
        film_health = await film.health()
        capabilities["filmcraft"] = {
            "version": film_health.version,
            "healthy": film_health.healthy,
            "errors": list(film_health.errors),
        }
        if not film_health.healthy:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "health",
                    "code": "ENGINE_UNHEALTHY",
                    "message": "; ".join(film_health.errors) or "FilmCraft is unhealthy",
                }
            )
            return finish("FAILED")
        engines_used.append("filmcraft")

        film_session = await film.open_session()
        report = film_session.report
        capabilities["filmcraft"].update(
            {
                "tool_names": report.tool_names,
                "tool_bindings": report.tool_bindings,
                "recipe_availability": report.recipe_availability,
                "schemas": {
                    name: report.tool_schemas[name]
                    for name in (
                        "command_list",
                        "command_run",
                        "render_preview",
                    )
                    if name in report.tool_schemas
                },
            }
        )
        required = {"project_inspect", "sequence_inspect", "command_list"}
        if not required.issubset(report.tool_names) or report.tool_bindings.get("command_run") is None:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "discover",
                    "code": "UNSUPPORTED_CAPABILITY",
                    "message": "FilmCraft lacks live project, sequence, or command discovery tools",
                }
            )
            return finish("FAILED")
        if report.preview_tool is None:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "discover",
                    "code": "UNSUPPORTED_CAPABILITY",
                    "message": "FilmCraft has no live sequence preview tool",
                }
            )
            return finish("FAILED")

        inspected = await film_session.invoke_tool("project_inspect", {})
        if not inspected.ok:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "inspect_project",
                    "code": inspected.error_code or "TOOL_CALL_FAILED",
                    "message": inspected.message,
                }
            )
            return finish("FAILED")
        project_payload = _json_payload(inspected.result)
        if not isinstance(project_payload, dict):
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "inspect_project",
                    "code": "INVALID_PROJECT_RESPONSE",
                    "message": "FilmCraft project_inspect returned no project object",
                }
            )
            return finish("FAILED")
        stages.append({"engine": "filmcraft", "stage": "inspect_project", "status": "complete"})

        sequence_result = await film_session.invoke_tool("sequence_inspect", {})
        sequence = _json_payload(sequence_result.result) if sequence_result.ok else None
        clip_id = _first_video_clip(sequence)
        if clip_id is None:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "inspect_sequence",
                    "code": "DEMO_SEQUENCE_REQUIRED",
                    "message": "FilmCraft demo has no active video clip to trim",
                }
            )
            return finish("FAILED")
        stages.append({"engine": "filmcraft", "stage": "inspect_sequence", "status": "complete"})

        trim_list = await film_session.invoke_tool(
            report.tool_bindings.get("command_list", "command_list"),
            {"filter": "trim", "enabled_only": True},
        )
        trim_command = _choose_trim_command(_json_payload(trim_list.result) if trim_list.ok else None)
        if trim_command is None:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "discover_timeline_edit",
                    "code": "UNSUPPORTED_CAPABILITY",
                    "message": "No unambiguous enabled trim command with live deltaFrames documentation",
                }
            )
            return finish("FAILED")
        edit_result = await film_session.execute(
            ExecuteRequest(
                arguments={
                    "id": trim_command["id"],
                    "params": {
                        "clip": clip_id,
                        "edge": "out",
                        "mode": "regular",
                        "deltaFrames": -1,
                    },
                }
            )
        )
        if not edit_result.ok:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "timeline_edit",
                    "code": edit_result.error_code or "TOOL_CALL_FAILED",
                    "message": edit_result.message,
                }
            )
            return finish("FAILED")
        capabilities["filmcraft"]["timeline_command"] = str(trim_command["id"])
        stages.append({"engine": "filmcraft", "stage": "timeline_edit", "status": "complete"})

        preview_tool = report.preview_tool
        preview_schema = report.tool_schemas.get(preview_tool, {}) if preview_tool else {}
        preview_arguments: dict[str, object] = {}
        preview_properties = preview_schema.get("properties", {})
        if "seconds" in preview_properties:
            preview_arguments["seconds"] = 1.0
        if "max_side" in preview_properties:
            preview_arguments["max_side"] = 960
        rendered = await film_session.preview(PreviewRequest(arguments=preview_arguments))
        preview_data = _preview_png(rendered) if rendered.ok else None
        if preview_data is None or not _is_png(preview_data):
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "preview",
                    "code": rendered.error_code or "INVALID_PREVIEW",
                    "message": rendered.message or "FilmCraft did not return a valid PNG frame",
                }
            )
            return finish("FAILED")
        preview_path = _unique_output(project, "previews", "video-smoke-frame.png")
        preview_path.write_bytes(preview_data)
        preview_paths.append(str(preview_path))
        stages.append({"engine": "filmcraft", "stage": "preview", "status": "complete"})

        status = "READY_FOR_REVIEW"
        effect_health = await effect.health()
        capabilities["effectcraft"] = {
            "version": effect_health.version,
            "healthy": effect_health.healthy,
            "errors": list(effect_health.errors),
        }
        if effect_health.healthy:
            engines_used.append("effectcraft")
            try:
                effect_session = await effect.open_session()
                effect_report = effect_session.report
                capabilities["effectcraft"].update(
                    {
                        "tool_names": effect_report.tool_names,
                        "tool_bindings": effect_report.tool_bindings,
                        "recipe_availability": effect_report.recipe_availability,
                        "schemas": {
                            "render_frame": effect_report.tool_schemas["render_frame"]
                        },
                    }
                )
                if "render_frame" not in effect_report.tool_names:
                    raise ProjectSafetyError("EffectCraft has no live render_frame tool")
                frame_path = _unique_output(project, "intermediates", "effectcraft-frame.png")
                properties = effect_report.tool_schemas.get("render_frame", {}).get("properties", {})
                frame_arguments: dict[str, object] = {"path": str(frame_path)}
                if "max_side" in properties:
                    frame_arguments["max_side"] = 640
                if "inline" in properties:
                    frame_arguments["inline"] = True
                effect_frame = await effect_session.invoke_tool("render_frame", frame_arguments)
                if not effect_frame.ok or not frame_path.is_file() or not _is_png(frame_path.read_bytes()):
                    raise ProjectSafetyError(
                        effect_frame.message or "EffectCraft did not write a valid rendered frame"
                    )
                capabilities["effectcraft"]["status"] = "HEALTHY"
                capabilities["effectcraft"]["frame_path"] = str(frame_path)
                stages.append(
                    {"engine": "effectcraft", "stage": "render_frame", "status": "complete"}
                )
            except Exception as exc:  # noqa: BLE001 - optional EffectCraft failures cannot block FilmCraft export.
                status = "PARTIAL_SUCCESS"
                errors.append(
                    {
                        "engine": "effectcraft",
                        "stage": "render_frame",
                        "code": "OPTIONAL_STAGE_FAILED",
                        "message": str(exc),
                    }
                )
                capabilities["effectcraft"]["status"] = "FAILED_OPTIONAL"
                stages.append(
                    {"engine": "effectcraft", "stage": "render_frame", "status": "failed_optional"}
                )
        else:
            capabilities["effectcraft"]["status"] = "SKIPPED_UNHEALTHY"
            stages.append(
                {"engine": "effectcraft", "stage": "render_frame", "status": "skipped_unhealthy"}
            )

        export_list = await film_session.invoke_tool(
            report.tool_bindings.get("command_list", "command_list"),
            {"filter": "Media", "enabled_only": True},
        )
        export_command = _choose_h264_export(
            _json_payload(export_list.result) if export_list.ok else None
        )
        if export_command is None:
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "discover_video_export",
                    "code": "UNSUPPORTED_CAPABILITY",
                    "message": "No unambiguous enabled H.264 media export command was discovered",
                }
            )
            return finish("FAILED")
        candidate = _unique_output(project, "intermediates", "video-smoke-candidate.mp4")
        export_result = await film_session.execute(
            ExecuteRequest(
                arguments={
                    "id": export_command["id"],
                    "params": {
                        "path": str(candidate),
                        "format": "h264",
                        "range": "custom",
                        "startSeconds": 0.0,
                        "endSeconds": 3.0,
                        "width": 640,
                        "height": 360,
                        "fps": 24.0,
                        "audio": False,
                        "wait": True,
                    },
                }
            )
        )
        if not export_result.ok or not _valid_mp4(candidate):
            errors.append(
                {
                    "engine": "filmcraft",
                    "stage": "candidate_export",
                    "code": export_result.error_code or "INVALID_MP4",
                    "message": export_result.message or "FilmCraft did not write a valid MP4 candidate",
                }
            )
            return finish("FAILED")
        capabilities["filmcraft"]["export_command"] = str(export_command["id"])
        candidate_paths.append(str(candidate))
        stages.append({"engine": "filmcraft", "stage": "candidate_export", "status": "complete"})
        stages.append({"engine": "ion", "stage": "ready_for_review", "status": "complete"})
        return finish(status)
    except (MCPClientError, OSError, ProjectSafetyError, ValueError, TypeError) as exc:
        errors.append(
            {
                "engine": "ion",
                "stage": "safety_or_runtime",
                "code": "VIDEO_WORKFLOW_FAILED",
                "message": str(exc),
            }
        )
        return finish("FAILED")
    finally:
        if effect_session is not None:
            await effect_session.close()
        if film_session is not None:
            await film_session.close()
