"""MCP entry point for Ion Creative's stable high-level tool surface."""

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from ion_creative.adapters.base import (
    AdapterResult,
    CraftAdapterSession,
    PreviewRequest,
    _schema_validation_error,
)
from ion_creative.adapters.designcraft import DesignCraftAdapter
from ion_creative.adapters.effectcraft import EffectCraftAdapter
from ion_creative.adapters.filmcraft import FilmCraftAdapter
from ion_creative.adapters.lightcraft import LightCraftAdapter
from ion_creative.adapters.pdfcraft import PdfCraftAdapter
from ion_creative.adapters.photocraft import PhotoCraftAdapter
from ion_creative.adapters.vectorcraft import VectorCraftAdapter
from ion_creative.asset_planning import inspect_asset_file, plan_workflow_data
from ion_creative.capabilities.catalog import collect_live_capabilities
from ion_creative.capabilities.taxonomy import normalized_capability_id
from ion_creative.mcp.stdio_client import MCPClientError
from ion_creative.models import EngineInspection, IonToolError, IonToolResult, RuntimeInspection
from ion_creative.project import (
    READABLE_AREAS,
    WRITABLE_AREAS,
    IonProject,
    ProjectSafetyError,
    create_named_project,
    create_project,
    export_finalized_artifact,
    finalize_artifact,
    safe_project_path,
    safe_project_read_path,
)
from ion_creative.recipes.image_smoke import (
    RecipeResult,
    _is_png,
    _preview_png,
    _response_object,
    _unique_output,
    run_image_smoke,
)
from ion_creative.recipes.video_smoke import run_video_smoke
from ion_creative.runtime.registry import load_runtime_specs

RUNTIME_ENGINES = (
    "photocraft",
    "filmcraft",
    "effectcraft",
    "lightcraft",
    "vectorcraft",
    "designcraft",
    "pdfcraft",
)
MAX_CAPABILITY_BATCH_STEPS = 25
_PATH_ARGUMENT_NAMES = frozenset(
    {
        "path",
        "paths",
        "file",
        "files",
        "filepath",
        "filepaths",
        "directory",
        "directories",
        "dir",
        "folder",
        "folders",
        "destination",
        "filename",
        "filenames",
        "uri",
        "uris",
    }
)


def _ion_error(engine: str, stage: str, code: str, message: str) -> IonToolError:
    return IonToolError(engine=engine, stage=stage, code=code, message=message)


def _health_errors(engine: str, messages: list[str]) -> list[IonToolError]:
    return [
        _ion_error(engine, "health", "ENGINE_HEALTH_CHECK_FAILED", message)
        for message in messages
    ]


def _runtime_adapter(engine: str, project_root: Path):
    if engine == "photocraft":
        return PhotoCraftAdapter(project_root)
    if engine == "filmcraft":
        return FilmCraftAdapter(demo=True, project_root=project_root)
    if engine == "effectcraft":
        return EffectCraftAdapter(demo=True, project_root=project_root)
    if engine == "lightcraft":
        return LightCraftAdapter(demo=True, project_root=project_root)
    if engine == "vectorcraft":
        return VectorCraftAdapter(project_root)
    if engine == "designcraft":
        return DesignCraftAdapter(project_root)
    if engine == "pdfcraft":
        return PdfCraftAdapter(project_root)
    raise ValueError(f"Unknown Ion runtime: {engine}")


def _capability_error(
    status: str,
    engine: str,
    capability_id: str,
    code: str,
    message: str,
    *,
    project_id: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": status,
        "engine": engine,
        "capability_id": capability_id,
        "errors": [
            {
                "engine": engine or "ion",
                "stage": "run_capability",
                "code": code,
                "message": message,
            }
        ],
    }
    if project_id is not None:
        result["project_id"] = project_id
    return result


def _is_filesystem_path_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    compound_file = re.search(r"(?:^|[^a-z0-9])files?$", key.casefold()) or re.search(
        r"Files?$", key
    )
    return normalized in _PATH_ARGUMENT_NAMES or normalized.endswith(
        (
            "filepath",
            "filepaths",
            "directory",
            "directories",
            "folder",
            "folders",
            "path",
            "paths",
            "uri",
            "uris",
        )
    ) or bool(compound_file)


def _is_property_path_operation(operation: str) -> bool:
    normalized = operation.casefold().replace("_", ".")
    return any(marker in normalized for marker in ("property", "keyframe", "attribute"))


def _readable_project_path(project: IonProject, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if candidate.parts and candidate.parts[0] in READABLE_AREAS:
            candidate = project.root / candidate
        else:
            candidate = project.inputs / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProjectSafetyError(f"Project input is missing or unreadable: {value}") from exc
    for area_name in READABLE_AREAS:
        area = getattr(project, area_name)
        if area.is_symlink() or not area.is_dir():
            raise ProjectSafetyError(f"Project media area must be a real directory: {area}")
        try:
            resolved.relative_to(area.resolve(strict=True))
        except ValueError:
            continue
        if resolved.is_file() or resolved.is_dir():
            return resolved
    raise ProjectSafetyError(f"Input path resolves outside the project media areas: {value}")


def _writable_project_path(project: IonProject, value: str, *, operation: str, key: str) -> Path:
    lowered = f"{operation} {key}".casefold()
    if any(marker in lowered for marker in ("export", "destination")):
        default_area = "exports"
    elif any(marker in lowered for marker in ("preview", "render", "frame")):
        default_area = "previews"
    else:
        default_area = "working"
    candidate = Path(value).expanduser()
    explicit_area: str | None = None
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        for area_name in WRITABLE_AREAS:
            if resolved.is_relative_to(getattr(project, area_name).resolve(strict=True)):
                explicit_area = area_name
                break
    elif candidate.parts and candidate.parts[0] in WRITABLE_AREAS:
        explicit_area = candidate.parts[0]
        candidate = project.root / candidate
    area = explicit_area or default_area
    return safe_project_path(project, candidate, area=area)


def _confine_capability_arguments(
    project: IonProject,
    raw_name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    """Map filesystem path arguments into the project and reject outside paths."""
    if raw_name in {"command_run", "execute_command", "run_command", "execute"}:
        operation = str(arguments.get("id", arguments.get("command", raw_name)))
    elif raw_name == "control_call":
        operation = str(arguments.get("method", raw_name))
    else:
        operation = raw_name
    if _is_property_path_operation(operation) or raw_name in {
        "get_property",
        "set_property",
        "add_keyframe",
    }:
        return arguments

    def visit(value: object, key: str = "", current_operation: str = operation) -> object:
        if isinstance(value, dict):
            if key == "step":
                step_operation = value.get("id", value.get("command"))
                if isinstance(step_operation, str):
                    current_operation = step_operation
            return {
                nested_key: visit(
                    nested_value,
                    str(nested_key),
                    current_operation,
                )
                for nested_key, nested_value in value.items()
            }
        if isinstance(value, list):
            child_key = "step" if key in {"steps", "commands"} else key
            return [visit(item, child_key, current_operation) for item in value]
        if isinstance(value, str) and _is_filesystem_path_key(key):
            if _is_property_path_operation(current_operation):
                return value
            lowered = f"{current_operation} {key}".casefold()
            if any(
                marker in lowered
                for marker in (
                    "save",
                    "export",
                    "destination",
                    "output",
                    "render",
                    "preview",
                    "write",
                    "delete",
                    "remove",
                    "move",
                    "rename",
                    "copy",
                    "overwrite",
                )
            ):
                return str(_writable_project_path(project, value, operation=current_operation, key=key))
            return str(_readable_project_path(project, value))
        return value

    confined = visit(arguments)
    if not isinstance(confined, dict):
        raise ProjectSafetyError("Capability arguments must be an object")
    return confined


async def _validate_live_command_ids(
    session: CraftAdapterSession,
    raw_name: str,
    arguments: dict[str, object],
) -> tuple[bool, str]:
    """Require nested commands to exist in this engine's live command listing."""
    command_tool = raw_name in {
        session.report.tool_bindings.get("command_run"),
        "command_run",
        "execute_command",
        "run_command",
        "execute",
    }
    batch_tool = raw_name in {
        session.report.tool_bindings.get("command_batch"),
        "command_batch",
        "batch",
        "run_commands",
        "run_command_batch",
    }
    if not command_tool and not batch_tool:
        return True, ""
    list_tool = session.report.tool_bindings.get("command_list")
    if list_tool is None:
        return True, ""
    cached_commands = getattr(session, "_ion_live_commands", None)
    if isinstance(cached_commands, dict):
        command_schemas = cached_commands
        available = getattr(session, "_ion_live_command_ids", set())
    else:
        list_arguments: dict[str, object] = {}
        list_schema = session.report.tool_schemas.get(list_tool, {})
        list_properties = list_schema.get("properties", {})
        if isinstance(list_properties, dict) and "schemas" in list_properties:
            list_arguments["schemas"] = True
        listed = await session.invoke_tool(list_tool, list_arguments)
        if not listed.ok:
            return False, "The engine failed to return its live command list"
        payload = _response_object(listed.result)
        command_items = payload.get("commands", payload.get("result", [])) if isinstance(payload, dict) else payload
        if not isinstance(command_items, list):
            return False, "The live command list did not return a command array"
        command_schemas = {}
        available: set[str] = set()
        for item in command_items:
            if not isinstance(item, dict):
                continue
            command_id = item.get("id", item.get("command"))
            if isinstance(command_id, str):
                available.add(command_id)
                schema = item.get("schema")
                if isinstance(schema, dict):
                    command_schemas[command_id] = schema
        session._ion_live_commands = command_schemas
        session._ion_live_command_ids = available
    requested: list[tuple[str, object]] = []
    if command_tool:
        command_id = arguments.get("id", arguments.get("command"))
        if isinstance(command_id, str):
            requested.append((command_id, arguments.get("params", {})))
    else:
        steps = arguments.get("steps", arguments.get("commands"))
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    return False, "Each command batch step must be an object"
                command_id = step.get("id", step.get("command"))
                if isinstance(command_id, str):
                    requested.append((command_id, step.get("params", {})))
                else:
                    return False, "Each command batch step must include a command ID"
    if not requested:
        return False, "A live command ID is required"
    for command_id, params in requested:
        if command_id not in available:
            return False, f"Command {command_id!r} was not present in the live command list"
        command_schema = command_schemas.get(command_id)
        if command_schema and isinstance(params, dict):
            validation_error = _schema_validation_error(params, command_schema, f"{command_id}.params")
            if validation_error:
                return False, validation_error
    return True, ""


def _compact_upstream_result(result: AdapterResult) -> dict[str, object]:
    value: dict[str, object] = {
        "ok": result.ok,
        "tool": result.tool,
        "error_code": result.error_code,
        "message": result.message,
        "content": [],
    }
    if result.result is None:
        return value
    content: list[dict[str, object]] = []
    for block in result.result.content:
        if getattr(block, "type", None) == "text":
            content.append({"type": "text", "text": str(getattr(block, "text", ""))[:3000]})
        elif getattr(block, "type", None) == "image":
            data = getattr(block, "data", "")
            content.append(
                {
                    "type": "image",
                    "mime_type": getattr(block, "mime_type", None),
                    "bytes_base64_length": len(data) if isinstance(data, str) else 0,
                    "payload_included": False,
                }
            )
        else:
            content.append({"type": str(getattr(block, "type", "unknown"))})
    value["content"] = content
    def compact(value: object, key: str = "", depth: int = 0) -> object:
        if depth > 8:
            return "<maximum nesting omitted>"
        if isinstance(value, dict):
            return {str(name): compact(item, str(name), depth + 1) for name, item in list(value.items())[:100]}
        if isinstance(value, list):
            items = [compact(item, key, depth + 1) for item in value[:100]]
            if len(value) > 100:
                items.append(f"<{len(value) - 100} additional items omitted>")
            return items
        if isinstance(value, str):
            if key.casefold() in {"data", "base64", "image_data", "payload"} and len(value) > 256:
                return {"payload_included": False, "characters": len(value)}
            return value[:3000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:3000]

    structured = getattr(result.result, "structured_content", None)
    if isinstance(structured, dict):
        value["structured_content"] = compact(structured)
    return value


async def _execute_capability_step(
    engine: str,
    capability_id: str,
    arguments: dict[str, object],
    project_id: str | None,
    *,
    preview_after: bool = False,
    export_after: bool = False,
    sessions: dict[tuple[str, str], CraftAdapterSession] | None = None,
    projects: dict[tuple[str, str], IonProject] | None = None,
) -> dict[str, object]:
    normalized_engine = engine.strip().casefold() if isinstance(engine, str) else ""
    normalized_id = capability_id.strip().casefold() if isinstance(capability_id, str) else ""
    selected_project_id = "default" if project_id is None else project_id
    if not normalized_engine or normalized_engine not in load_runtime_specs():
        return _capability_error(
            "INVALID_ENGINE",
            normalized_engine,
            normalized_id,
            "UNKNOWN_RUNTIME",
            f"Unknown Ion runtime: {engine}",
            project_id=selected_project_id,
        )
    if not normalized_id:
        return _capability_error(
            "INVALID_ARGUMENTS",
            normalized_engine,
            normalized_id,
            "INVALID_CAPABILITY_ID",
            "capability_id must be a non-empty normalized ID",
            project_id=selected_project_id,
        )
    if not isinstance(arguments, dict):
        return _capability_error(
            "INVALID_ARGUMENTS",
            normalized_engine,
            normalized_id,
            "INVALID_ARGUMENTS",
            "arguments must be an object",
            project_id=selected_project_id,
        )

    cache_key = (normalized_engine, str(selected_project_id))
    session: CraftAdapterSession | None = None
    owns_session = sessions is None
    try:
        if projects is not None and cache_key in projects:
            project = projects[cache_key]
        else:
            project = create_named_project(selected_project_id)
            if projects is not None:
                projects[cache_key] = project
        if sessions is not None:
            session = sessions.get(cache_key)
        if session is None:
            adapter = _runtime_adapter(normalized_engine, project.root)
            session = await adapter.open_session()
            if sessions is not None:
                sessions[cache_key] = session
        report = session.report
        if not report.healthy:
            return _capability_error(
                "UNAVAILABLE",
                normalized_engine,
                normalized_id,
                "ENGINE_UNAVAILABLE",
                "; ".join(report.errors) or f"{normalized_engine} did not pass live MCP discovery",
                project_id=selected_project_id,
            )
        matches = [
            tool
            for tool in report.tools
            if normalized_capability_id(normalized_engine, tool.name) == normalized_id
        ]
        if not matches:
            return _capability_error(
                "UNSUPPORTED_CAPABILITY",
                normalized_engine,
                normalized_id,
                "CAPABILITY_NOT_DISCOVERED",
                f"{normalized_id} is not present in the current live {normalized_engine} tool list",
                project_id=selected_project_id,
            )
        if len(matches) != 1:
            return _capability_error(
                "UNAVAILABLE",
                normalized_engine,
                normalized_id,
                "AMBIGUOUS_CAPABILITY_ID",
                "More than one live tool maps to this normalized capability ID",
                project_id=selected_project_id,
            )
        raw_name = matches[0].name
        try:
            safe_arguments = _confine_capability_arguments(project, raw_name, arguments)
        except ProjectSafetyError as exc:
            return _capability_error(
                "FAILED",
                normalized_engine,
                normalized_id,
                "PROJECT_PATH_REJECTED",
                str(exc),
                project_id=selected_project_id,
            )

        command_is_valid, command_message = await _validate_live_command_ids(
            session,
            raw_name,
            safe_arguments,
        )
        if not command_is_valid:
            return _capability_error(
                "INVALID_ARGUMENTS",
                normalized_engine,
                normalized_id,
                "INVALID_COMMAND_ARGUMENTS"
                if ".params" in command_message
                else "COMMAND_NOT_DISCOVERED",
                command_message,
                project_id=selected_project_id,
            )

        upstream = await session.invoke_tool(raw_name, safe_arguments)
        if upstream.ok:
            status = "SUCCESS"
        elif upstream.error_code in {"INVALID_ARGUMENTS", "UNSUPPORTED_ARGUMENTS"}:
            status = "INVALID_ARGUMENTS"
        else:
            status = "FAILED"
        preview: dict[str, object] | None = None
        if preview_after and not upstream.ok:
            preview = {
                "status": "SKIPPED",
                "message": "Preview was skipped because the capability did not complete successfully.",
            }
        elif preview_after:
            preview_result = upstream if raw_name == report.preview_tool else None
            if preview_result is None and report.preview_tool:
                preview_result = await session.invoke_tool(report.preview_tool, {})
            preview_data = _preview_png(preview_result) if preview_result else None
            if preview_data is not None and _is_png(preview_data):
                filename = re.sub(r"[^a-zA-Z0-9_-]+", "-", normalized_id).strip("-")
                destination = _unique_output(project, "previews", f"{filename or 'capability'}-preview.png")
                destination.write_bytes(preview_data)
                preview = {"status": "SUCCESS", "path": str(destination)}
            else:
                preview = {
                    "status": "UNAVAILABLE",
                    "message": "No live preview tool returned a valid PNG image.",
                }
                if upstream.ok:
                    status = "PARTIAL_SUCCESS"
        response: dict[str, object] = {
            "status": status,
            "engine": normalized_engine,
            "capability_id": normalized_id,
            "raw_name": raw_name,
            "project_id": selected_project_id,
            "project_root": str(project.root),
            "result": _compact_upstream_result(upstream),
            "qa_needed": bool(preview_after or export_after),
        }
        if preview_after:
            response["preview"] = preview or {"status": "UNAVAILABLE"}
        if export_after:
            response["export"] = {
                "status": "REQUIRES_FINALIZE" if upstream.ok else "NOT_READY",
                "message": (
                    "Use Ion finalize after visual review, then export the unchanged finalized artifact."
                    if upstream.ok
                    else "The capability did not complete, so no artifact is ready to finalize or export."
                ),
            }
        if not upstream.ok:
            response["errors"] = [
                {
                    "engine": normalized_engine,
                    "stage": "execute",
                    "code": upstream.error_code or "TOOL_CALL_FAILED",
                    "message": upstream.message,
                }
            ]
        return response
    except (MCPClientError, OSError, ProjectSafetyError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
        if isinstance(exc, ProjectSafetyError):
            status = "FAILED"
            error_code = "PROJECT_PATH_REJECTED"
        elif isinstance(exc, ValueError) and str(exc).startswith("Unknown Ion runtime:"):
            status = "UNAVAILABLE"
            error_code = "ADAPTER_UNAVAILABLE"
        elif isinstance(exc, (MCPClientError, OSError, TimeoutError)):
            status = "UNAVAILABLE"
            error_code = getattr(exc, "code", "MCP_PROCESS_FAILED")
        else:
            status = "FAILED"
            error_code = "CAPABILITY_EXECUTION_FAILED"
        return _capability_error(
            status,
            normalized_engine,
            normalized_id,
            error_code,
            f"{type(exc).__name__}: {exc}",
            project_id=selected_project_id,
        )
    finally:
        if owns_session and session is not None:
            await session.close()


async def _inspect_engine(engine: str) -> EngineInspection:
    try:
        with tempfile.TemporaryDirectory(prefix="ion-creative-health-") as temporary_root:
            health = await _runtime_adapter(engine, Path(temporary_root)).health()
        errors = _health_errors(engine, health.errors)
        if not health.healthy and not errors:
            errors.append(
                _ion_error(
                    engine,
                    "health",
                    "ENGINE_HEALTH_CHECK_FAILED",
                    "Health check reported unhealthy without details",
                )
            )
        return EngineInspection(
            engine=engine,
            version=health.version,
            healthy=health.healthy,
            capabilities=health.capabilities,
            capability_count=len(health.capabilities),
            errors=errors,
        )
    except (MCPClientError, OSError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
        try:
            spec = load_runtime_specs().get(engine)
            registry_failure = ""
        except (OSError, ValueError, TypeError, KeyError) as registry_exc:
            spec = None
            registry_failure = (
                "; runtime registry unavailable "
                f"({type(registry_exc).__name__}: {registry_exc})"
            )
        return EngineInspection(
            engine=engine,
            version=spec.version if spec else None,
            healthy=False,
            errors=[
                _ion_error(
                    engine,
                    "health",
                    "RUNTIME_INSPECTION_FAILED",
                    f"{type(exc).__name__}: {exc}{registry_failure}",
                )
            ],
        )


def _unhealthy_workflow_result(tool: str, engine: EngineInspection) -> IonToolResult:
    return IonToolResult(
        status="DEGRADED",
        tool=tool,
        message=f"{engine.engine} is unhealthy; the workflow was not started.",
        engines=[engine.engine],
        exact_versions={engine.engine: engine.version} if engine.version else {},
        qa_needed=False,
        errors=engine.errors
        or [
            _ion_error(
                engine.engine,
                "health",
                "ENGINE_HEALTH_CHECK_FAILED",
                "Required engine health check failed",
            )
        ],
    )


def _not_ready(tool: str) -> IonToolResult:
    return IonToolResult(
        status="NOT_READY",
        tool=tool,
        message="This Ion Creative workflow is not implemented yet.",
    )


def _asset_project(project_root: str | None) -> IonProject:
    if project_root is not None:
        return create_project(Path(project_root))
    return create_named_project(None)


def _asset_tool_error(tool: str, code: str, message: str) -> dict[str, object]:
    return {
        "status": "FAILED",
        "tool": tool,
        "message": message,
        "errors": [
            {
                "engine": "ion",
                "stage": tool,
                "code": code,
                "message": message,
            }
        ],
    }


def _recipe_tool_result(tool: str, result: RecipeResult) -> IonToolResult:
    errors = [IonToolError.model_validate(item) for item in result.errors]
    candidates = list(result.candidate_paths)
    return IonToolResult(
        status=result.status,
        tool=tool,
        message=(
            "Candidate is ready for visual review."
            if result.status in {"READY_FOR_REVIEW", "PARTIAL_SUCCESS"}
            else "Workflow did not produce a reviewable candidate."
        ),
        output_paths=candidates + result.output_paths,
        engines=result.engines_used,
        exact_versions=result.exact_versions,
        preview_paths=result.preview_paths,
        qa_needed=result.qa_needed,
        errors=errors,
    )


def _locate_video_preview(project, artifact: Path) -> IonToolResult | None:
    if project.ion.is_symlink() or not project.ion.is_dir():
        return None
    for filename, recipe in (
        ("video-smoke-report.json", "video_smoke"),
        ("social-video-report.json", "social_video"),
    ):
        report_path = project.ion / filename
        if report_path.is_symlink() or not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or report.get("recipe") != recipe:
                continue
            if report.get("status") not in {"READY_FOR_REVIEW", "PARTIAL_SUCCESS"}:
                continue
            candidate_paths = report.get("candidate_paths")
            preview_paths = report.get("preview_paths")
            if not isinstance(candidate_paths, list) or not isinstance(preview_paths, list):
                continue
            if not all(isinstance(path, str) for path in candidate_paths + preview_paths):
                continue
            candidates = [safe_project_read_path(project, candidate) for candidate in candidate_paths]
            if artifact not in candidates or not preview_paths:
                continue
            preview = safe_project_read_path(project, preview_paths[0])
            if not _is_png(preview.read_bytes()):
                continue
            engines = report.get("engines_used", [])
            versions = report.get("exact_versions", {})
            return IonToolResult(
                status="SUCCESS",
                tool="preview",
                message="FilmCraft's rendered frame is ready for visual review.",
                output_paths=[str(artifact)],
                engines=engines if isinstance(engines, list) else [],
                exact_versions=versions if isinstance(versions, dict) else {},
                preview_paths=[str(preview)],
                qa_needed=True,
            )
        except (OSError, ProjectSafetyError, ValueError):
            continue
    return None


async def _render_project_preview(project_root: str, artifact_path: str) -> IonToolResult:
    project = create_project(Path(project_root))
    artifact = safe_project_read_path(project, artifact_path)
    if artifact.suffix.casefold() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        located = _locate_video_preview(project, artifact)
        if located is not None:
            return located
        return IonToolResult(
            status="UNSUPPORTED_CAPABILITY",
            tool="preview",
            message="No matching rendered video frame is recorded for this artifact.",
            errors=[
                _ion_error(
                    "filmcraft",
                    "preview",
                    "VIDEO_PREVIEW_NOT_FOUND",
                    "No matching rendered video frame is recorded for this artifact.",
                )
            ],
        )
    adapter = PhotoCraftAdapter(project.root)
    health = await adapter.health()
    if not health.healthy:
        return IonToolResult(
            status="DEGRADED",
            tool="preview",
            message="PhotoCraft is not ready to render a preview.",
            engines=["photocraft"],
            exact_versions={"photocraft": health.version},
            errors=_health_errors("photocraft", health.errors)
            or [
                _ion_error(
                    "photocraft",
                    "health",
                    "ENGINE_HEALTH_CHECK_FAILED",
                    "Health check reported unhealthy without details",
                )
            ],
        )

    session = await adapter.open_session()
    try:
        relative_path = artifact.relative_to(project.root).as_posix()
        opened = await session.invoke_tool("doc_open", {"path": relative_path})
        if not opened.ok:
            return IonToolResult(
                status="FAILED",
                tool="preview",
                message="PhotoCraft could not open the requested project artifact.",
                engines=["photocraft"],
                exact_versions={"photocraft": health.version},
                errors=[
                    _ion_error(
                        "photocraft",
                        "open",
                        opened.error_code or "TOOL_CALL_FAILED",
                        opened.message,
                    )
                ],
            )

        preview_tool = session.report.preview_tool
        preview_schema = (
            session.report.tool_schemas.get(preview_tool, {}) if preview_tool else {}
        )
        preview_arguments = {}
        if "max_side" in preview_schema.get("properties", {}):
            preview_arguments["max_side"] = 1024
        rendered = await session.preview(PreviewRequest(arguments=preview_arguments))
        if not rendered.ok:
            return IonToolResult(
                status="FAILED",
                tool="preview",
                message="PhotoCraft failed to render the requested preview.",
                engines=["photocraft"],
                exact_versions={"photocraft": health.version},
                errors=[
                    _ion_error(
                        "photocraft",
                        "preview",
                        rendered.error_code or "TOOL_CALL_FAILED",
                        rendered.message,
                    )
                ],
            )
        preview_data = _preview_png(rendered)
        if preview_data is None or not _is_png(preview_data):
            return IonToolResult(
                status="FAILED",
                tool="preview",
                message="PhotoCraft did not return a valid PNG preview.",
                engines=["photocraft"],
                exact_versions={"photocraft": health.version},
                errors=[
                    _ion_error(
                        "photocraft",
                        "preview",
                        "INVALID_PREVIEW",
                        "PhotoCraft did not return a valid PNG preview.",
                    )
                ],
            )

        preview = _unique_output(project, "previews", f"{artifact.stem}-preview.png")
        preview.write_bytes(preview_data)
        return IonToolResult(
            status="SUCCESS",
            tool="preview",
            message="Preview rendered for visual review.",
            engines=["photocraft"],
            exact_versions={"photocraft": health.version},
            preview_paths=[str(preview)],
            qa_needed=True,
        )
    finally:
        await session.close()


def create_server() -> MCPServer:
    """Build the Ion Creative MCP server without starting a transport."""
    server = MCPServer(
        "ion-creative",
        version="0.1.0",
        instructions="Use Ion Creative's high-level tools for local creative workflows.",
    )

    @server.tool(description="Inspect a media asset within an explicitly granted project root.")
    def inspect_asset(asset_path: str, project_root: str | None = None) -> dict[str, object]:
        try:
            project = _asset_project(project_root)
            source = _readable_project_path(project, asset_path)
            return inspect_asset_file(source)
        except ProjectSafetyError as exc:
            return _asset_tool_error("inspect_asset", "PROJECT_PATH_REJECTED", str(exc))
        except (OSError, ValueError) as exc:
            return _asset_tool_error(
                "inspect_asset",
                "ASSET_INSPECTION_FAILED",
                f"{type(exc).__name__}: {exc}",
            )

    @server.tool(description="Inspect installed Ion runtime versions and discovered capabilities.")
    async def inspect_runtime(engine: str | None = None) -> RuntimeInspection:
        selected = engine.strip().casefold() if engine else None
        if selected is not None and selected not in RUNTIME_ENGINES:
            unknown = EngineInspection(
                engine=selected,
                healthy=False,
                errors=[
                    _ion_error(
                        selected,
                        "inspect_runtime",
                        "UNKNOWN_RUNTIME",
                        f"Unknown Ion runtime: {selected}",
                    )
                ],
            )
            return RuntimeInspection(
                status="DEGRADED",
                engines=[unknown],
                message=f"Unknown Ion runtime: {selected}",
            )
        engines = (selected,) if selected else RUNTIME_ENGINES
        inspections = await asyncio.gather(*(_inspect_engine(name) for name in engines))
        try:
            from ion_creative.desktop.launchers import verify_launchers

            launcher_results = {
                str(item.get("engine")): item
                for item in verify_launchers()
                if isinstance(item, dict) and isinstance(item.get("engine"), str)
            }
        except (OSError, TypeError, ValueError, RuntimeError):
            launcher_results = {}
        for inspection in inspections:
            launcher = launcher_results.get(inspection.engine)
            if launcher is None:
                inspection.launcher_state = "UNKNOWN"
            elif launcher.get("verified") is True:
                inspection.launcher_state = "VERIFIED"
            elif launcher.get("installed") is True:
                inspection.launcher_state = "UNVERIFIED"
            else:
                inspection.launcher_state = "MISSING"
        healthy = all(item.healthy for item in inspections)
        return RuntimeInspection(
            status="HEALTHY" if healthy else "DEGRADED",
            engines=inspections,
            message=(
                "All selected Craft runtimes are healthy."
                if healthy
                else "One or more selected Craft runtimes are degraded."
            ),
        )

    @server.tool(description="Plan a supported workflow from an intent and input assets.")
    def plan_workflow(
        intent: str,
        asset_paths: list[str] | None = None,
        project_root: str | None = None,
    ) -> dict[str, object]:
        asset_context: list[dict[str, object]] = []
        if asset_paths:
            try:
                project = _asset_project(project_root)
                for asset_path in asset_paths:
                    source = _readable_project_path(project, asset_path)
                    asset_context.append(inspect_asset_file(source))
            except ProjectSafetyError as exc:
                return _asset_tool_error("plan_workflow", "PROJECT_PATH_REJECTED", str(exc))
            except (OSError, ValueError) as exc:
                return _asset_tool_error(
                    "plan_workflow",
                    "ASSET_INSPECTION_FAILED",
                    f"{type(exc).__name__}: {exc}",
                )
        return plan_workflow_data(intent, asset_context)

    @server.tool(description="Run one bounded, preview-first workflow using live Craft capabilities.")
    async def run_workflow(
        workflow_id: str,
        project_root: str,
        source_paths: list[str] | None = None,
        brief: str | None = None,
    ) -> dict[str, object]:
        from ion_creative.workflows import WORKFLOWS
        from ion_creative.workflows.common import failed, failure_from_exception, input_path

        selected = workflow_id.strip().casefold() if isinstance(workflow_id, str) else ""
        if selected not in WORKFLOWS:
            return failed(
                selected,
                "ion",
                "validation",
                "UNKNOWN_WORKFLOW",
                f"Unknown guided workflow: {workflow_id}",
                status="INVALID_WORKFLOW",
            )
        paths = source_paths or []
        if not isinstance(paths, list) or any(not isinstance(value, str) or not value.strip() for value in paths):
            return failed(selected, "ion", "validation", "INVALID_SOURCE_PATHS", "source_paths must be a list of non-empty project input paths.")
        if selected in {"personal_brand_portrait", "pdf_ops"} and len(paths) > 1:
            return failed(selected, "ion", "validation", "TOO_MANY_SOURCES", "This workflow accepts at most one source path.")
        if selected == "personal_brand_portrait" and len(paths) != 1:
            return failed(selected, "ion", "validation", "SOURCE_REQUIRED", "The portrait workflow requires exactly one source image in inputs/.")
        if selected == "social_video" and len(paths) < 2:
            return failed(selected, "ion", "validation", "TWO_SOURCES_REQUIRED", "The social-video workflow requires at least two clips in inputs/.")
        try:
            project = create_project(Path(project_root))
            checked_paths = [input_path(project, value) for value in paths]
            workflow = WORKFLOWS[selected]
            if selected == "personal_brand_portrait":
                return await workflow(project, str(checked_paths[0]))
            if selected == "social_video":
                return await workflow(project, [str(path) for path in checked_paths])
            if selected == "pdf_ops":
                return await workflow(project, str(checked_paths[0]) if checked_paths else None, brief)
            return await workflow(project, brief)
        except (MCPClientError, OSError, ProjectSafetyError, ValueError, TypeError, RuntimeError) as exc:
            return failure_from_exception(selected, exc)

    @server.tool(description="List live-discovered Craft capabilities with optional taxonomy filters.")
    async def list_capabilities(
        engine: str | None = None,
        category: str | None = None,
        search_query: str | None = None,
        supports_preview: bool | None = None,
        supports_export: bool | None = None,
    ) -> dict[str, object]:
        return await collect_live_capabilities(
            engine=engine,
            category=category,
            search_query=search_query,
            supports_preview=supports_preview,
            supports_export=supports_export,
            adapter_factory=_runtime_adapter,
        )

    @server.tool(description="Execute one live-discovered Craft capability inside an Ion project.")
    async def run_capability(
        engine: str,
        capability_id: str,
        arguments: dict[str, Any],
        project_id: str | None = None,
        preview_after: bool = False,
        export_after: bool = False,
    ) -> dict[str, object]:
        return await _execute_capability_step(
            engine,
            capability_id,
            arguments,
            project_id,
            preview_after=preview_after,
            export_after=export_after,
        )

    @server.tool(description="Execute a bounded ordered batch of live Craft capabilities.")
    async def run_capability_batch(
        steps: list[dict[str, Any]],
        project_id: str | None = None,
        stop_on_error: bool = True,
    ) -> dict[str, object]:
        if not isinstance(steps, list) or not steps:
            return {
                "status": "INVALID_ARGUMENTS",
                "steps": [],
                "errors": [{"engine": "ion", "stage": "validation", "code": "EMPTY_BATCH", "message": "steps must contain at least one capability"}],
            }
        if len(steps) > MAX_CAPABILITY_BATCH_STEPS:
            return {
                "status": "INVALID_ARGUMENTS",
                "steps": [],
                "errors": [{"engine": "ion", "stage": "validation", "code": "BATCH_LIMIT_EXCEEDED", "message": f"A capability batch is limited to {MAX_CAPABILITY_BATCH_STEPS} steps"}],
            }

        sessions: dict[tuple[str, str], CraftAdapterSession] = {}
        projects: dict[tuple[str, str], IonProject] = {}
        outcomes: list[dict[str, object]] = []
        try:
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    outcome: dict[str, object] = {
                        "status": "INVALID_ARGUMENTS",
                        "errors": [{"engine": "ion", "stage": "validation", "code": "INVALID_BATCH_STEP", "message": "Each batch step must be an object"}],
                    }
                else:
                    engine = step.get("engine")
                    capability_id = step.get("capability_id")
                    arguments = step.get("arguments", {})
                    outcome = await _execute_capability_step(
                        engine if isinstance(engine, str) else "",
                        capability_id if isinstance(capability_id, str) else "",
                        arguments,
                        step.get("project_id", project_id),
                        preview_after=step.get("preview_after") is True,
                        export_after=step.get("export_after") is True,
                        sessions=sessions,
                        projects=projects,
                    )
                outcome["step"] = index + 1
                outcomes.append(outcome)
                if outcome.get("status") not in {"SUCCESS", "PARTIAL_SUCCESS"} and stop_on_error:
                    for skipped_index in range(index + 1, len(steps)):
                        outcomes.append(
                            {
                                "step": skipped_index + 1,
                                "status": "SKIPPED",
                                "message": "An earlier batch step failed and stop_on_error is enabled",
                            }
                        )
                    break
        finally:
            for session in sessions.values():
                await session.close()

        successes = sum(outcome.get("status") in {"SUCCESS", "PARTIAL_SUCCESS"} for outcome in outcomes)
        failures = any(outcome.get("status") not in {"SUCCESS", "PARTIAL_SUCCESS", "SKIPPED"} for outcome in outcomes)
        if failures and successes:
            status = "PARTIAL_SUCCESS"
        elif failures:
            status = "FAILED"
        else:
            status = "SUCCESS"
        return {
            "status": status,
            "project_id": project_id or "default",
            "steps": outcomes,
            "qa_needed": any(outcome.get("qa_needed") is True for outcome in outcomes),
        }

    @server.tool(description="Render or locate a preview for visual quality review.")
    async def preview(project_root: str, artifact_path: str) -> IonToolResult:
        try:
            return await _render_project_preview(project_root, artifact_path)
        except (MCPClientError, OSError, ProjectSafetyError, ValueError) as exc:
            return IonToolResult(
                status="FAILED",
                tool="preview",
                message="Preview request failed its project safety checks.",
                errors=[_ion_error("ion", "preview", "REQUEST_FAILED", str(exc))],
            )

    @server.tool(description="Apply a supported image edit inside an Ion project workspace.")
    async def edit_image(project_root: str, source_path: str, instructions: str) -> IonToolResult:
        try:
            health = await _inspect_engine("photocraft")
            if not health.healthy:
                return _unhealthy_workflow_result("edit_image", health)
            project = create_project(Path(project_root))
            result = await run_image_smoke(project, source_path, instructions=instructions)
            return _recipe_tool_result("edit_image", result)
        except (OSError, ProjectSafetyError, ValueError) as exc:
            return IonToolResult(
                status="FAILED",
                tool="edit_image",
                message="Image edit request failed its project safety checks.",
                errors=[_ion_error("ion", "edit_image", "REQUEST_FAILED", str(exc))],
            )

    @server.tool(description="Apply a supported photographic grade inside an Ion project workspace.")
    def grade_image(project_root: str, source_path: str, look: str) -> IonToolResult:
        return IonToolResult(
            status="UNSUPPORTED_CAPABILITY",
            tool="grade_image",
            message="Wave 1 does not yet expose a verified LightCraft grading recipe.",
            engines=["lightcraft"],
            errors=[
                _ion_error(
                    "lightcraft",
                    "grade",
                    "UNSUPPORTED_CAPABILITY",
                    f"Unsupported grading look: {look}",
                )
            ],
        )

    @server.tool(description="Apply a supported video timeline edit inside an Ion project workspace.")
    async def edit_video(project_root: str, source_paths: list[str], instructions: str) -> IonToolResult:
        if source_paths or instructions.strip().casefold() not in {"demo", "smoke"}:
            return IonToolResult(
                status="UNSUPPORTED_CAPABILITY",
                tool="edit_video",
                message="Wave 1 supports only the disposable FilmCraft demo edit recipe.",
                errors=[
                    _ion_error(
                        "filmcraft",
                        "validation",
                        "UNSUPPORTED_CAPABILITY",
                        "This tool does not yet accept user source media or arbitrary edit instructions.",
                    )
                ],
            )
        try:
            health = await _inspect_engine("filmcraft")
            if not health.healthy:
                return _unhealthy_workflow_result("edit_video", health)
            project = create_project(Path(project_root))
            result = await run_video_smoke(project)
            return _recipe_tool_result("edit_video", result)
        except (MCPClientError, OSError, ProjectSafetyError, ValueError) as exc:
            return IonToolResult(
                status="FAILED",
                tool="edit_video",
                message="Video edit request failed its project safety checks.",
                errors=[_ion_error("ion", "edit_video", "REQUEST_FAILED", str(exc))],
            )

    @server.tool(description="Apply a supported grade to a video workflow.")
    def grade_video(project_root: str, source_path: str, look: str) -> IonToolResult:
        return IonToolResult(
            status="UNSUPPORTED_CAPABILITY",
            tool="grade_video",
            message="Wave 1 has no verified video grading recipe.",
            errors=[
                _ion_error(
                    "filmcraft",
                    "grade",
                    "UNSUPPORTED_CAPABILITY",
                    f"Unsupported video look: {look}",
                )
            ],
        )

    @server.tool(description="Assemble a supported social video workflow.")
    def make_social_video(project_root: str, source_paths: list[str], platform: str) -> IonToolResult:
        return IonToolResult(
            status="UNSUPPORTED_CAPABILITY",
            tool="make_social_video",
            message="Wave 1 has no verified social-video preset for the requested inputs.",
            errors=[
                _ion_error(
                    "filmcraft",
                    "social_video",
                    "UNSUPPORTED_CAPABILITY",
                    f"Unsupported platform or source combination: {platform}",
                )
            ],
        )

    @server.tool(description="Mark a reviewed workflow candidate ready for export.")
    def finalize(project_root: str, artifact_path: str) -> IonToolResult:
        try:
            project = create_project(Path(project_root))
            receipt = finalize_artifact(project, artifact_path)
            return IonToolResult(
                status="FINALIZED",
                tool="finalize",
                message="Visual review was recorded for this unchanged candidate.",
                output_paths=[str(project.root / receipt["path"])],
                qa_needed=False,
            )
        except (OSError, ProjectSafetyError, ValueError) as exc:
            return IonToolResult(
                status="FAILED",
                tool="finalize",
                message="Finalization failed its project safety checks.",
                errors=[_ion_error("ion", "finalize", "REQUEST_FAILED", str(exc))],
            )

    @server.tool(description="Export a finalized artifact into the Ion project exports directory.")
    def export(project_root: str, artifact_path: str, output_name: str) -> IonToolResult:
        try:
            project = create_project(Path(project_root))
            exported = export_finalized_artifact(project, artifact_path, output_name)
            return IonToolResult(
                status="EXPORTED",
                tool="export",
                message="Finalized artifact was copied into the project exports directory.",
                output_paths=[str(exported)],
                qa_needed=False,
            )
        except (OSError, ProjectSafetyError, ValueError) as exc:
            return IonToolResult(
                status="FAILED",
                tool="export",
                message="Export failed its project safety checks.",
                errors=[_ion_error("ion", "export", "REQUEST_FAILED", str(exc))],
            )

    return server


def main() -> None:
    """Run the MCP server over stdio for local Codex plugin hosts."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
