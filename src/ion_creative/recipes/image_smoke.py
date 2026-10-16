"""Safe, deterministic PhotoCraft image smoke workflow."""

import base64
import json
import os
import struct
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ion_creative.adapters.base import CapabilityReport, ExecuteRequest, PreviewRequest
from ion_creative.adapters.photocraft import PhotoCraftAdapter
from ion_creative.mcp.stdio_client import MCPClientError
from ion_creative.project import (
    IonProject,
    ProjectSafetyError,
    safe_input_path,
    safe_project_path,
    sha256_file,
)
from ion_creative.runtime.registry import load_runtime_specs

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SUPPORTED_EDIT_INTENTS = {"invert", "invert colors"}


@dataclass(frozen=True, slots=True)
class RecipeResult:
    recipe: str
    status: str
    project_root: str
    engines_used: list[str] = field(default_factory=list)
    exact_versions: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    candidate_paths: list[str] = field(default_factory=list)
    output_paths: list[str] = field(default_factory=list)
    preview_paths: list[str] = field(default_factory=list)
    source_path: str | None = None
    source_hash_before: str | None = None
    source_hash_after: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    stages: list[dict[str, str]] = field(default_factory=list)
    report_path: str | None = None
    qa_needed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _error(engine: str, stage: str, code: str, message: str) -> dict[str, str]:
    return {"engine": engine, "stage": stage, "code": code, "message": message}


def _unique_output(project: IonProject, area: str, filename: str) -> Path:
    original = Path(filename)
    for index in range(1000):
        name = original.name if index == 0 else f"{original.stem}-{index:03d}{original.suffix}"
        candidate = safe_project_path(project, name, area=area)
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ProjectSafetyError(f"Unable to allocate an unused {area} output name for {filename}")


def _is_png(data: bytes) -> bool:
    return (
        len(data) >= 24
        and data.startswith(PNG_SIGNATURE)
        and all(dimension > 0 for dimension in struct.unpack(">II", data[16:24]))
    )


def _response_object(result: object | None) -> object | None:
    if result is None:
        return None
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = [
        block.text
        for block in getattr(result, "content", [])
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    ]
    if not texts:
        return None
    text = "\n".join(texts).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _choose_invert_command(payload: object) -> tuple[str | None, str]:
    if isinstance(payload, dict):
        entries = payload.get("commands", payload.get("result", []))
    else:
        entries = payload
    if not isinstance(entries, list):
        return None, "PhotoCraft command_list returned no command array"
    candidates = [
        item
        for item in entries
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and str(item.get("label", "")).strip().casefold() == "invert"
        and item.get("enabled") is True
        and item.get("params") in (None, "", "{}")
    ]
    image_menu_candidates = [
        item
        for item in candidates
        if isinstance(item.get("menu"), list) and "Image" in item["menu"]
    ]
    selected = image_menu_candidates if image_menu_candidates else candidates
    if len(selected) == 1:
        return selected[0]["id"], ""
    if not selected:
        return None, "PhotoCraft exposes no enabled parameter-free Invert command"
    return None, "PhotoCraft exposes multiple enabled Invert commands with no unambiguous image action"


def _preview_png(adapter_result: object) -> bytes | None:
    response = getattr(adapter_result, "result", None)
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) != "image":
            continue
        mime = getattr(block, "mimeType", getattr(block, "mime_type", ""))
        encoded = getattr(block, "data", None)
        if mime != "image/png" or not isinstance(encoded, str):
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        if _is_png(decoded):
            return decoded
    return None


def _write_recipe_report(project: IonProject, result: RecipeResult) -> RecipeResult:
    report_path = project.ion / "image-smoke-report.json"
    if project.ion.is_symlink() or not project.ion.is_dir() or report_path.is_symlink():
        raise ProjectSafetyError("Ion report directory or report path is not safe")
    temporary = project.ion / f".image-smoke-{uuid.uuid4().hex}.tmp"
    payload = replace(result, report_path=str(report_path)).to_dict()
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, report_path)
    finally:
        temporary.unlink(missing_ok=True)
    return replace(result, report_path=str(report_path))


def _tool_arguments(report: CapabilityReport, tool_name: str, path: str) -> dict[str, object]:
    schema = report.tool_schemas.get(tool_name, {})
    properties = schema.get("properties", {})
    if "path" not in properties:
        raise ProjectSafetyError(f"PhotoCraft {tool_name} has no documented path argument")
    arguments: dict[str, object] = {"path": path}
    if "format" in properties:
        arguments["format"] = "png"
    return arguments


async def run_image_smoke(
    project: IonProject,
    source_path: Path | str | None = None,
    *,
    instructions: str = "invert",
    photo_adapter=None,
    lightcraft_stage=None,
) -> RecipeResult:
    """Edit an immutable input and return a previewed candidate for visual review."""
    specs = load_runtime_specs()
    exact_versions = {name: spec.version for name, spec in specs.items()}
    adapter = photo_adapter or PhotoCraftAdapter(project.root)
    engines_used = ["photocraft"]
    stages: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    capabilities: dict[str, Any] = {}
    candidate_paths: list[str] = []
    output_paths: list[str] = []
    preview_paths: list[str] = []
    source: Path | None = None
    source_before: str | None = None
    source_after: str | None = None
    session = None

    def finish(status: str) -> RecipeResult:
        nonlocal source_after
        if source is not None and source_before is not None and source_after is None:
            try:
                source_after = sha256_file(source)
                if source_after != source_before:
                    errors.append(
                        _error(
                            "ion",
                            "source_integrity",
                            "SOURCE_MUTATED",
                            "Input hash changed during image smoke",
                        )
                    )
                    status = "FAILED"
            except OSError as exc:
                errors.append(_error("ion", "source_integrity", "SOURCE_HASH_FAILED", str(exc)))
                status = "FAILED"
        result = RecipeResult(
            recipe="image_smoke",
            status=status,
            project_root=str(project.root),
            engines_used=list(dict.fromkeys(engines_used)),
            exact_versions=exact_versions,
            capabilities=capabilities,
            candidate_paths=candidate_paths,
            output_paths=output_paths,
            preview_paths=preview_paths,
            source_path=str(source) if source else None,
            source_hash_before=source_before,
            source_hash_after=source_after,
            errors=errors,
            stages=stages,
        )
        return _write_recipe_report(project, result)

    if instructions.strip().casefold() not in SUPPORTED_EDIT_INTENTS:
        errors.append(
            _error("photocraft", "edit", "UNSUPPORTED_CAPABILITY", "Wave 1 supports only an invert edit")
        )
        return finish("FAILED")

    try:
        if source_path is None:
            inputs = sorted(path for path in project.inputs.iterdir() if path.is_file())
            if not inputs:
                errors.append(
                    _error("ion", "inspect", "INPUT_REQUIRED", "Image smoke requires one file in inputs/")
                )
                return finish("FAILED")
            source = safe_input_path(project, inputs[0])
        else:
            source = safe_input_path(project, source_path)
        source_before = sha256_file(source)
        health = await adapter.health()
        if not getattr(health, "healthy", False):
            message = "; ".join(getattr(health, "errors", [])) or "PhotoCraft runtime is unhealthy"
            errors.append(_error("photocraft", "health", "ENGINE_UNHEALTHY", message))
            return finish("FAILED")
        session = await adapter.open_session()
        report = session.report
        capabilities["photocraft"] = {
            "version": report.version,
            "tool_names": report.tool_names,
            "tool_bindings": report.tool_bindings,
            "recipe_availability": report.recipe_availability,
            "schemas": {
                name: report.tool_schemas[name]
                for name in ("doc_open", "command_list", "command_run", "doc_export")
                if name in report.tool_schemas
            },
        }
        required_tools = {"doc_open", "command_list", "command_run"}
        missing = sorted(required_tools - set(report.tool_names))
        if report.preview_tool is None:
            missing.append("render_preview")
        export_tool = next(
            (name for name in ("doc_export", "doc_save") if name in report.tool_names), None
        )
        if export_tool is None:
            missing.append("doc_export|doc_save")
        if missing:
            errors.append(
                _error(
                    "photocraft",
                    "discover",
                    "UNSUPPORTED_CAPABILITY",
                    f"Missing image workflow tools: {', '.join(missing)}",
                )
            )
            return finish("FAILED")

        open_path = source.relative_to(project.root).as_posix()
        opened = await session.invoke_tool("doc_open", {"path": open_path})
        if not opened.ok:
            errors.append(_error("photocraft", "open", opened.error_code or "TOOL_CALL_FAILED", opened.message))
            return finish("FAILED")
        stages.append({"engine": "photocraft", "stage": "open", "status": "complete"})

        command_list_tool = report.tool_bindings.get("command_list", "command_list")
        command_list_schema = report.tool_schemas.get(command_list_tool, {})
        command_list_properties = command_list_schema.get("properties", {})
        command_list_arguments: dict[str, object] = {}
        if "filter" in command_list_properties:
            command_list_arguments["filter"] = "invert"
        if "enabled_only" in command_list_properties:
            command_list_arguments["enabled_only"] = True
        listed = await session.invoke_tool(command_list_tool, command_list_arguments)
        if not listed.ok:
            errors.append(
                _error(
                    "photocraft",
                    "command_discovery",
                    listed.error_code or "TOOL_CALL_FAILED",
                    listed.message,
                )
            )
            return finish("FAILED")
        command_id, command_error = _choose_invert_command(_response_object(listed.result))
        if command_id is None:
            errors.append(
                _error("photocraft", "command_discovery", "UNSUPPORTED_CAPABILITY", command_error)
            )
            return finish("FAILED")
        executed = await session.execute(ExecuteRequest(arguments={"id": command_id}))
        if not executed.ok:
            errors.append(
                _error(
                    "photocraft",
                    "edit",
                    executed.error_code or "TOOL_CALL_FAILED",
                    executed.message,
                )
            )
            return finish("FAILED")
        stages.append(
            {"engine": "photocraft", "stage": "visible_edit", "status": "complete"}
        )

        preview_name = report.preview_tool
        preview_schema = report.tool_schemas.get(preview_name, {}) if preview_name else {}
        preview_arguments: dict[str, object] = {}
        if "max_side" in preview_schema.get("properties", {}):
            preview_arguments["max_side"] = 1024
        rendered = await session.preview(PreviewRequest(arguments=preview_arguments))
        if not rendered.ok:
            errors.append(
                _error(
                    "photocraft",
                    "preview",
                    rendered.error_code or "TOOL_CALL_FAILED",
                    rendered.message,
                )
            )
            return finish("FAILED")
        preview_data = _preview_png(rendered)
        if preview_data is None:
            errors.append(
                _error("photocraft", "preview", "INVALID_PREVIEW", "PhotoCraft returned no valid PNG preview")
            )
            return finish("FAILED")
        preview = _unique_output(project, "previews", "image-smoke.png")
        preview.write_bytes(preview_data)
        preview_paths.append(str(preview))
        stages.append({"engine": "photocraft", "stage": "preview", "status": "complete"})

        candidate = _unique_output(project, "intermediates", "image-smoke-candidate.png")
        export_arguments = _tool_arguments(report, export_tool, candidate.relative_to(project.root).as_posix())
        exported = await session.invoke_tool(export_tool, export_arguments)
        if not exported.ok:
            errors.append(
                _error(
                    "photocraft",
                    "candidate_export",
                    exported.error_code or "TOOL_CALL_FAILED",
                    exported.message,
                )
            )
            return finish("FAILED")
        if not candidate.is_file() or not _is_png(candidate.read_bytes()):
            errors.append(
                _error("photocraft", "candidate_export", "INVALID_EXPORT", "PhotoCraft did not write a valid PNG")
            )
            return finish("FAILED")
        stages.append({"engine": "photocraft", "stage": "candidate_export", "status": "complete"})

        selected_output = candidate
        status = "READY_FOR_REVIEW"
        if lightcraft_stage is None:
            capabilities["lightcraft"] = {
                "version": exact_versions["lightcraft"],
                "status": "SKIPPED_OPTIONAL",
            }
            stages.append({"engine": "lightcraft", "stage": "grade_preview", "status": "skipped_optional"})
        else:
            engines_used.append("lightcraft")
            try:
                light_result = await lightcraft_stage.run(project, candidate)
                if getattr(light_result, "ok", True) is False:
                    raise RuntimeError(getattr(light_result, "message", "LightCraft stage failed"))
                proposed = getattr(light_result, "output_path", None)
                if proposed is not None:
                    selected_output = Path(proposed).resolve(strict=True)
                    allowed = any(
                        selected_output.is_relative_to(getattr(project, area).resolve(strict=True))
                        for area in ("working", "intermediates")
                    )
                    if not allowed or not _is_png(selected_output.read_bytes()):
                        raise ProjectSafetyError("LightCraft output must be a valid PNG inside the project")
                capabilities["lightcraft"] = {
                    "version": exact_versions["lightcraft"],
                    "status": "HEALTHY",
                }
                stages.append(
                    {"engine": "lightcraft", "stage": "grade_preview", "status": "complete"}
                )
            except Exception as exc:  # noqa: BLE001 - optional stage failures must preserve the candidate.
                status = "PARTIAL_SUCCESS"
                errors.append(_error("lightcraft", "grade_preview", "OPTIONAL_STAGE_FAILED", str(exc)))
                capabilities["lightcraft"] = {
                    "version": exact_versions["lightcraft"],
                    "status": "FAILED_OPTIONAL",
                }
                stages.append(
                    {"engine": "lightcraft", "stage": "grade_preview", "status": "failed_optional"}
                )

        if not _is_png(selected_output.read_bytes()):
            errors.append(
                _error("ion", "candidate", "INVALID_CANDIDATE", "Image candidate is not a valid PNG")
            )
            return finish("FAILED")
        candidate_paths.append(str(selected_output))
        stages.append({"engine": "ion", "stage": "ready_for_review", "status": "complete"})
        source_after = sha256_file(source)
        if source_after != source_before:
            errors.append(
                _error("ion", "source_integrity", "SOURCE_MUTATED", "Input hash changed during image smoke")
            )
            return finish("FAILED")
        stages.append({"engine": "ion", "stage": "source_integrity", "status": "unchanged"})
        return finish(status)
    except (MCPClientError, OSError, ProjectSafetyError, ValueError) as exc:
        errors.append(_error("ion", "safety", "PROJECT_SAFETY_ERROR", str(exc)))
        return finish("FAILED")
    finally:
        if session is not None:
            await session.close()
