"""Live discovery and normalization of Craft MCP tools."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ion_creative.adapters.base import CapabilityReport, ToolDescriptor
from ion_creative.capabilities.taxonomy import (
    capability_category,
    normalized_capability_id,
    summarize_argument_schema,
)
from ion_creative.runtime.registry import RuntimeSpec, load_runtime_specs

AdapterFactory = Callable[[str, Path], Any]


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    engine: str
    raw_name: str
    capability_id: str
    category: str
    summary: str
    argument_schema_summary: dict[str, Any]
    supports_preview: bool
    supports_export: bool
    export_side_effects: list[str]
    availability: str
    engine_health: str
    discovered_at: str
    upstream_source: dict[str, str]
    input_schema: dict[str, Any]

    def to_dict(self, *, include_schema: bool = False) -> dict[str, Any]:
        result = asdict(self)
        if not include_schema:
            result.pop("input_schema")
        return result


def _default_adapter_factory(engine: str, project_root: Path) -> Any:
    """Use the same adapter routing as Ion's runtime inspection tool."""
    from ion_creative.server import _runtime_adapter

    return _runtime_adapter(engine, project_root)


def _summary(tool: ToolDescriptor) -> str:
    text = " ".join(tool.description.split())
    if text:
        return text[:240]
    return tool.name.replace("_", " ").replace(".", " ").strip().capitalize()


def _supports_export(tool: ToolDescriptor) -> bool:
    source = f"{tool.name} {tool.description}".casefold()
    if "export" in source:
        return True
    words = set(re.sub(r"[^a-z0-9]+", " ", source).split())
    return bool(words & {"save", "write", "writes", "writing"}) and bool(
        words
        & {
            "artifact",
            "document",
            "disk",
            "doc",
            "file",
            "graphic",
            "image",
            "layout",
            "media",
            "output",
            "pdf",
            "photo",
            "project",
            "video",
        }
    )


def _record(spec: RuntimeSpec, report: CapabilityReport, tool: ToolDescriptor) -> CapabilityRecord:
    supports_export = _supports_export(tool)
    return CapabilityRecord(
        engine=spec.engine,
        raw_name=tool.name,
        capability_id=normalized_capability_id(spec.engine, tool.name),
        category=capability_category(tool.name, tool.description),
        summary=_summary(tool),
        argument_schema_summary=summarize_argument_schema(tool.input_schema),
        supports_preview=(
            tool.name == report.preview_tool
            or "preview" in tool.name.casefold()
            or "render" in tool.name.casefold()
        ),
        supports_export=supports_export,
        export_side_effects=(
            ["May write an output file; use a project-scoped destination."] if supports_export else []
        ),
        availability="AVAILABLE" if report.healthy else "DEGRADED",
        engine_health=report.status,
        discovered_at=datetime.now(UTC).isoformat(),
        upstream_source={
            "repository": f"https://github.com/{spec.github_owner}/{spec.github_repo}",
            "tag": spec.tag,
            "runtime_version": spec.version,
            "mcp_tool_name": tool.name,
        },
        input_schema=tool.input_schema,
    )


async def collect_live_capabilities(
    *,
    engine: str | None = None,
    category: str | None = None,
    search_query: str | None = None,
    supports_preview: bool | None = None,
    supports_export: bool | None = None,
    adapter_factory: AdapterFactory | None = None,
    include_schemas: bool = False,
) -> dict[str, Any]:
    """Discover live tool schemas and return normalized capability records."""
    specs = load_runtime_specs()
    selected_engine = engine.strip().casefold() if engine else None
    if selected_engine is not None and selected_engine not in specs:
        return {
            "status": "INVALID_ENGINE",
            "message": f"Unknown Ion runtime: {selected_engine}",
            "engines": [],
            "capabilities": [],
        }

    selected = [selected_engine] if selected_engine else list(specs)
    factory = adapter_factory or _default_adapter_factory
    engine_results: list[dict[str, Any]] = []
    records: list[CapabilityRecord] = []
    for name in selected:
        spec = specs[name]
        try:
            with tempfile.TemporaryDirectory(prefix=f"ion-creative-{name}-catalog-") as root:
                adapter = factory(name, Path(root))
                report = await adapter.discover()
            if not isinstance(report, CapabilityReport):
                raise TypeError("Craft adapter returned an invalid capability report")
            discovered = [_record(spec, report, tool) for tool in report.tools]
            records.extend(discovered)
            engine_results.append(
                {
                    "engine": name,
                    "version": spec.version,
                    "status": report.status,
                    "capability_count": len(discovered),
                    "error_code": "MCP_DISCOVERY_DEGRADED" if report.errors else None,
                    "errors": list(report.errors),
                }
            )
        except (OSError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
            engine_results.append(
                {
                    "engine": name,
                    "version": spec.version,
                    "status": "UNAVAILABLE",
                    "capability_count": 0,
                    "error_code": "ADAPTER_UNAVAILABLE",
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }
            )

    statuses = [result["status"] for result in engine_results]
    if statuses and all(status == "HEALTHY" for status in statuses):
        status = "HEALTHY"
    elif any(status in {"HEALTHY", "DEGRADED"} for status in statuses):
        status = "PARTIAL"
    else:
        status = "UNAVAILABLE"
    selected_category = category.strip().casefold() if category else None
    selected_query = search_query.strip().casefold() if search_query else None
    if selected_category or selected_query or supports_preview is not None or supports_export is not None:
        records = [
            record
            for record in records
            if (selected_category is None or record.category.casefold() == selected_category)
            and (
                selected_query is None
                or selected_query
                in f"{record.engine} {record.raw_name} {record.capability_id} {record.category} {record.summary}".casefold()
            )
            and (supports_preview is None or record.supports_preview is supports_preview)
            and (supports_export is None or record.supports_export is supports_export)
        ]
        for result in engine_results:
            result["capability_count"] = sum(record.engine == result["engine"] for record in records)
    return {
        "status": status,
        "engines": engine_results,
        "capabilities": [record.to_dict(include_schema=include_schemas) for record in records],
    }
