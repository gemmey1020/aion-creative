"""Shared safety, live-discovery, and result helpers for guided workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ion_creative.adapters.base import CraftAdapterSession, ExecuteRequest
from ion_creative.project import (
    READABLE_AREAS,
    IonProject,
    ProjectSafetyError,
    safe_input_path,
)
from ion_creative.recipes.image_smoke import _response_object, _unique_output


class WorkflowFailure(RuntimeError):
    def __init__(self, engine: str, stage: str, code: str, message: str):
        super().__init__(message)
        self.engine = engine
        self.stage = stage
        self.code = code


def result(
    workflow_id: str,
    *,
    status: str,
    message: str,
    engines: list[str] | None = None,
    exact_versions: dict[str, str] | None = None,
    output_paths: list[str] | None = None,
    preview_paths: list[str] | None = None,
    stages: list[dict[str, str]] | None = None,
    errors: list[dict[str, str]] | None = None,
    source_hashes: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "tool": "run_workflow",
        "workflow_id": workflow_id,
        "message": message,
        "engines": engines or [],
        "exact_versions": exact_versions or {},
        "output_paths": output_paths or [],
        "preview_paths": preview_paths or [],
        "stages": stages or [],
        "errors": errors or [],
        "source_hashes": source_hashes or {},
        "details": details or {},
        "qa_needed": status in {"READY_FOR_REVIEW", "PARTIAL_SUCCESS"},
    }


def failed(
    workflow_id: str,
    engine: str,
    stage: str,
    code: str,
    message: str,
    *,
    status: str = "FAILED",
    engines: list[str] | None = None,
    exact_versions: dict[str, str] | None = None,
    source_hashes: dict[str, str] | None = None,
) -> dict[str, object]:
    return result(
        workflow_id,
        status=status,
        message=message,
        engines=engines,
        exact_versions=exact_versions,
        source_hashes=source_hashes,
        errors=[
            {
                "engine": engine,
                "stage": stage,
                "code": code,
                "message": message,
            }
        ],
    )


def failure_from_exception(
    workflow_id: str,
    exc: Exception,
    *,
    fallback_engine: str = "ion",
    fallback_stage: str = "workflow",
    fallback_code: str = "WORKFLOW_FAILED",
) -> dict[str, object]:
    if isinstance(exc, WorkflowFailure):
        return failed(workflow_id, exc.engine, exc.stage, exc.code, str(exc))
    if isinstance(exc, ProjectSafetyError):
        return failed(workflow_id, "ion", "safety", "PROJECT_PATH_REJECTED", str(exc))
    return failed(
        workflow_id,
        fallback_engine,
        fallback_stage,
        fallback_code,
        f"{type(exc).__name__}: {exc}",
    )


def input_path(project: IonProject, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if candidate.parts and candidate.parts[0] in READABLE_AREAS:
            candidate = project.root / candidate
        else:
            candidate = project.inputs / candidate
    source = safe_input_path(project, candidate)
    if not source.is_file():
        raise ProjectSafetyError(f"Workflow input is not a regular file: {value}")
    return source


def ensure_tool(report: object, name: str, engine: str) -> str:
    names = getattr(report, "tool_names", [])
    bindings = getattr(report, "tool_bindings", {})
    actual_name = bindings.get(name, name) if isinstance(bindings, dict) else name
    if actual_name not in names:
        raise WorkflowFailure(engine, "discovery", "UNSUPPORTED_CAPABILITY", f"{engine} has no live `{name}` tool")
    return actual_name


async def invoke_tool(
    session: CraftAdapterSession,
    report: object,
    engine: str,
    name: str,
    arguments: dict[str, object],
):
    actual_name = ensure_tool(report, name, engine)
    outcome = await session.invoke_tool(actual_name, arguments)
    if not outcome.ok:
        raise WorkflowFailure(
            engine,
            name,
            outcome.error_code or "TOOL_CALL_FAILED",
            outcome.message or f"{engine} `{actual_name}` failed",
        )
    return outcome


async def live_command_id(
    session: CraftAdapterSession,
    report: object,
    engine: str,
    expected_id: str,
) -> str:
    command_tool = ensure_tool(report, "command_list", engine)
    schemas = getattr(report, "tool_schemas", {})
    properties = schemas.get(command_tool, {}).get("properties", {}) if isinstance(schemas, dict) else {}
    arguments: dict[str, object] = {}
    if "filter" in properties:
        arguments["filter"] = expected_id
    if "enabled_only" in properties:
        arguments["enabled_only"] = True
    listed = await session.invoke_tool(command_tool, arguments)
    if not listed.ok:
        raise WorkflowFailure(
            engine,
            "command_discovery",
            listed.error_code or "COMMAND_DISCOVERY_FAILED",
            listed.message or f"{engine} command discovery failed",
        )
    payload = _response_object(listed.result)
    entries = payload.get("commands", payload.get("result", [])) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise WorkflowFailure(
            engine,
            "command_discovery",
            "INVALID_COMMAND_LIST",
            f"{engine} returned no command list",
        )
    match = next(
        (
            item
            for item in entries
            if isinstance(item, dict)
            and item.get("id") == expected_id
            and item.get("enabled") is True
        ),
        None,
    )
    if match is None:
        raise WorkflowFailure(
            engine,
            "command_discovery",
            "UNSUPPORTED_CAPABILITY",
            f"{engine} did not live-discover enabled command `{expected_id}`",
        )
    return expected_id


async def execute_live_command(
    session: CraftAdapterSession,
    report: object,
    engine: str,
    command_id: str,
    params: dict[str, object],
):
    live_id = await live_command_id(session, report, engine, command_id)
    outcome = await session.execute(
        ExecuteRequest(arguments={"id": live_id, "params": params})
    )
    if not outcome.ok:
        raise WorkflowFailure(
            engine,
            "execute",
            outcome.error_code or "COMMAND_FAILED",
            outcome.message or f"{engine} command `{live_id}` failed",
        )
    return outcome


def payload(outcome: object) -> object | None:
    return _response_object(getattr(outcome, "result", None))


def preview_bytes(outcome: object) -> bytes | None:
    from ion_creative.recipes.image_smoke import _preview_png

    return _preview_png(outcome)


def write_preview(project: IonProject, filename: str, data: bytes) -> Path:
    path = _unique_output(project, "previews", filename)
    path.write_bytes(data)
    return path


def project_relative(project: IonProject, path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(project.root.resolve(strict=True)).as_posix()
    except ValueError as exc:
        raise ProjectSafetyError(f"Workflow destination escapes the project: {path}") from exc


def file_hashes(paths: list[Path]) -> dict[str, str]:
    import hashlib

    values: dict[str, str] = {}
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        values[str(path)] = digest.hexdigest()
    return values


def source_hashes_unchanged(paths: list[Path], before: dict[str, str]) -> bool:
    return file_hashes(paths) == before
