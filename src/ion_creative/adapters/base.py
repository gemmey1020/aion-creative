"""Shared discovery and normalized operations for Craft MCP adapters."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from mcp.types import CallToolResult

from ion_creative.mcp.stdio_client import MCPClientError, StdioMCPClient, open_stdio_mcp
from ion_creative.paths import runtime_dir
from ion_creative.runtime.doctor import RuntimeStatus, doctor_engine
from ion_creative.runtime.registry import RuntimeSpec, load_runtime_specs

CORE_TOOL_NAMES = (
    "command_list",
    "command_run",
    "command_batch",
    "doc_inspect",
    "render_preview",
)

TOOL_ALIASES = {
    "photocraft": {"render_preview": ("doc_render_preview",)},
    "filmcraft": {},
    "effectcraft": {
        "command_list": ("list_commands",),
        "command_run": ("execute_command",),
        "command_batch": ("batch",),
        "doc_inspect": ("get_project",),
        "render_preview": ("render_frame",),
    },
    "vectorcraft": {},
    "designcraft": {
        "command_list": ("list_commands",),
        "command_run": ("execute",),
        "command_batch": ("batch",),
        "doc_inspect": ("inspect_document",),
        "render_preview": ("render_page",),
    },
    "pdfcraft": {},
    "lightcraft": {},
}

ENGINE_RECIPES = {
    "photocraft": {"image_edit": CORE_TOOL_NAMES},
    "filmcraft": {"video_edit": CORE_TOOL_NAMES, "social_video": CORE_TOOL_NAMES},
    "effectcraft": {"image_effect": CORE_TOOL_NAMES},
    "lightcraft": {"image_grade": CORE_TOOL_NAMES},
    "vectorcraft": {"vector_graphic": CORE_TOOL_NAMES},
    "designcraft": {"design_layout": CORE_TOOL_NAMES},
    "pdfcraft": {"pdf_ops": CORE_TOOL_NAMES},
}


def _schema_type_matches(value: object, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return True


def _schema_validation_error(value: object, schema: dict[str, Any], path: str) -> str | None:
    schema_type = schema.get("type")
    allowed_types = schema_type if isinstance(schema_type, list) else [schema_type]
    allowed_types = [item for item in allowed_types if isinstance(item, str)]
    if allowed_types and not any(_schema_type_matches(value, item) for item in allowed_types):
        expected = " or ".join(allowed_types)
        return f"{path} must be {expected}"

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path} must be one of: {', '.join(map(str, enum))}"

    properties = schema.get("properties")
    if isinstance(value, dict):
        if not isinstance(properties, dict):
            properties = {}
        required = schema.get("required", [])
        missing = [key for key in required if isinstance(key, str) and key not in value] if isinstance(required, list) else []
        if missing:
            return f"{path} requires: {', '.join(missing)}"
        for key, item in value.items():
            item_schema = properties.get(key)
            if isinstance(item_schema, dict):
                error = _schema_validation_error(item, item_schema, f"{path}.{key}")
                if error:
                    return error
            elif schema.get("additionalProperties") is False:
                return f"{path} does not accept: {key}"
            elif isinstance(schema.get("additionalProperties"), dict):
                error = _schema_validation_error(item, schema["additionalProperties"], f"{path}.{key}")
                if error:
                    return error

    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict) and "$ref" not in items:
        for index, item in enumerate(value):
            error = _schema_validation_error(item, items, f"{path}[{index}]")
            if error:
                return error
    return None


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]

    @classmethod
    def from_tool(cls, tool: object) -> ToolDescriptor:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("MCP tools/list returned a tool without a name")
        schema = getattr(tool, "input_schema", getattr(tool, "inputSchema", {}))
        if not isinstance(schema, dict):
            schema = {}
        description = getattr(tool, "description", None)
        return cls(name, description if isinstance(description, str) else "", schema)


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    engine: str
    version: str
    status: str
    tool_names: list[str] = field(default_factory=list)
    tools: list[ToolDescriptor] = field(default_factory=list)
    tool_bindings: dict[str, str] = field(default_factory=dict)
    missing_tools: list[str] = field(default_factory=list)
    preview_tool: str | None = None
    recipe_availability: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.status == "HEALTHY"

    @property
    def tool_schemas(self) -> dict[str, dict[str, Any]]:
        return {tool.name: tool.input_schema for tool in self.tools}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    engine: str
    version: str
    healthy: bool
    mcp_initialized: bool
    capabilities: list[str] = field(default_factory=list)
    unavailable_recipes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    capability_report: CapabilityReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InspectRequest:
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class ExecuteRequest:
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class BatchRequest:
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdapterResult:
    engine: str
    tool: str
    ok: bool
    result: CallToolResult | None = None
    error_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.result is not None:
            value["result"] = self.result.model_dump(mode="json", by_alias=True)
        return value


InspectResult = AdapterResult
ExecuteResult = AdapterResult
BatchResult = AdapterResult
PreviewResult = AdapterResult


class CraftAdapter(Protocol):
    async def discover(self) -> CapabilityReport: ...

    async def health(self) -> AdapterHealth: ...

    async def inspect(self, request: InspectRequest) -> InspectResult: ...

    async def execute(self, request: ExecuteRequest) -> ExecuteResult: ...

    async def batch(self, request: BatchRequest) -> BatchResult: ...

    async def preview(self, request: PreviewRequest) -> PreviewResult: ...


class CraftAdapterSession:
    """One initialized Craft MCP process shared by a multi-step workflow."""

    def __init__(
        self,
        adapter: BaseCraftAdapter,
        client: StdioMCPClient,
        report: CapabilityReport,
        context: AbstractAsyncContextManager[StdioMCPClient],
    ) -> None:
        self.adapter = adapter
        self.client = client
        self.report = report
        self._context = context
        self._closed = False

    async def invoke_tool(self, name: str, arguments: dict[str, object]) -> AdapterResult:
        if name not in self.report.tool_names:
            return AdapterResult(
                engine=self.adapter.spec.engine,
                tool=name,
                ok=False,
                error_code="MISSING_CAPABILITY",
                message=f"{self.adapter.spec.engine} does not expose {name}",
            )
        schema = self.report.tool_schemas[name]
        validation_error = _schema_validation_error(arguments, schema, name)
        if validation_error:
            return AdapterResult(
                engine=self.adapter.spec.engine,
                tool=name,
                ok=False,
                error_code="INVALID_ARGUMENTS",
                message=validation_error,
            )
        result = await self.client.call_tool(name, arguments)
        is_error = getattr(result, "is_error", False)
        return AdapterResult(
            engine=self.adapter.spec.engine,
            tool=name,
            ok=not is_error,
            result=result,
            error_code="TOOL_CALL_FAILED" if is_error else None,
            message="Craft tool call failed" if is_error else "Craft tool call completed",
        )

    async def _invoke(self, canonical_tool: str, arguments: dict[str, object]) -> AdapterResult:
        upstream_tool = self.report.tool_bindings.get(canonical_tool)
        if upstream_tool is None:
            return AdapterResult(
                engine=self.adapter.spec.engine,
                tool=canonical_tool,
                ok=False,
                error_code="MISSING_CAPABILITY",
                message=f"{self.adapter.spec.engine} does not expose {canonical_tool}",
            )
        try:
            upstream_arguments = self.adapter._adapt_arguments(canonical_tool, arguments)
        except (TypeError, ValueError) as exc:
            return AdapterResult(
                engine=self.adapter.spec.engine,
                tool=canonical_tool,
                ok=False,
                error_code="UNSUPPORTED_ARGUMENTS",
                message=str(exc),
            )
        return await self.invoke_tool(upstream_tool, upstream_arguments)

    async def execute(self, request: ExecuteRequest) -> ExecuteResult:
        return await self._invoke("command_run", request.arguments)

    async def preview(self, request: PreviewRequest) -> PreviewResult:
        return await self._invoke("render_preview", request.arguments)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._context.__aexit__(None, None, None)


class BaseCraftAdapter:
    """Runtime-aware adapter that relies only on the child's live tool list."""

    def __init__(
        self,
        engine: str,
        *,
        args: list[str],
        cwd=None,
        project_root=None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        specs = load_runtime_specs()
        self.spec: RuntimeSpec = specs[engine]
        self.command = runtime_dir(engine, self.spec.version) / "bin" / self.spec.cli_binary
        self.args = list(args)
        self.cwd = cwd
        self.project_root = project_root
        self.env_overrides = dict(env_overrides or {})

    def _make_report(
        self,
        tools: list[ToolDescriptor],
        *,
        errors: list[str] | None = None,
    ) -> CapabilityReport:
        names = sorted({tool.name for tool in tools})
        aliases = TOOL_ALIASES[self.spec.engine]
        bindings: dict[str, str] = {}
        for required_name in CORE_TOOL_NAMES:
            candidates = (required_name, *aliases.get(required_name, ()))
            actual_name = next((candidate for candidate in candidates if candidate in names), None)
            if actual_name is not None:
                bindings[required_name] = actual_name
        missing = sorted(set(CORE_TOOL_NAMES) - set(bindings))
        recipes = {
            recipe: set(required).issubset(bindings)
            for recipe, required in ENGINE_RECIPES[self.spec.engine].items()
        }
        has_error = bool(errors)
        return CapabilityReport(
            engine=self.spec.engine,
            version=self.spec.version,
            status="HEALTHY" if not missing and not has_error else "DEGRADED",
            tool_names=names,
            tools=sorted(tools, key=lambda tool: tool.name),
            tool_bindings=bindings,
            missing_tools=missing,
            preview_tool=bindings.get("render_preview"),
            recipe_availability=recipes,
            errors=list(errors or []),
        )

    async def _discover_with_client(self, client) -> CapabilityReport:
        if not client.initialized:
            return self._make_report([], errors=["MCP client did not complete initialization"])
        listing = await client.list_tools()
        try:
            tools = [ToolDescriptor.from_tool(tool) for tool in listing.tools]
        except (AttributeError, TypeError, ValueError) as exc:
            return self._make_report([], errors=[f"Invalid tools/list response: {exc}"])
        return self._make_report(tools)

    async def open_session(self) -> CraftAdapterSession:
        """Open one child process for a workflow that needs persistent document state."""
        context = open_stdio_mcp(
            self.command,
            self.args,
            cwd=self.cwd,
            env_overrides=self.env_overrides,
        )
        client = await context.__aenter__()
        try:
            report = await self._discover_with_client(client)
        except BaseException as exc:
            await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return CraftAdapterSession(self, client, report, context)

    async def discover(self) -> CapabilityReport:
        """Initialize the installed child and derive capabilities from tools/list."""
        try:
            async with open_stdio_mcp(
                self.command,
                self.args,
                cwd=self.cwd,
                env_overrides=self.env_overrides,
            ) as client:
                return await self._discover_with_client(client)
        except MCPClientError as exc:
            return self._make_report([], errors=[f"{exc.code}: {exc.message}"])
        except OSError as exc:
            return self._make_report([], errors=[f"MCP process could not start: {exc}"])

    async def health(self) -> AdapterHealth:
        runtime: RuntimeStatus = doctor_engine(self.spec.engine)
        if not runtime.healthy:
            return AdapterHealth(
                engine=self.spec.engine,
                version=self.spec.version,
                healthy=False,
                mcp_initialized=False,
                errors=[runtime.message],
            )
        report = await self.discover()
        unavailable = [name for name, available in report.recipe_availability.items() if not available]
        errors = list(report.errors)
        if report.missing_tools:
            errors.append(f"Missing required MCP tools: {', '.join(report.missing_tools)}")
        return AdapterHealth(
            engine=self.spec.engine,
            version=self.spec.version,
            healthy=runtime.healthy and report.healthy,
            mcp_initialized=not report.errors,
            capabilities=report.tool_names,
            unavailable_recipes=unavailable,
            errors=errors,
            capability_report=report,
        )

    async def invoke_tool(self, name: str, arguments: dict[str, object]) -> AdapterResult:
        """Invoke a non-core tool only after verifying its live name and input schema."""
        session = None
        try:
            session = await self.open_session()
            return await session.invoke_tool(name, arguments)
        except MCPClientError as exc:
            return AdapterResult(
                engine=self.spec.engine,
                tool=name,
                ok=False,
                error_code=exc.code,
                message=exc.message,
            )
        except OSError as exc:
            return AdapterResult(
                engine=self.spec.engine,
                tool=name,
                ok=False,
                error_code="MCP_PROCESS_FAILED",
                message=str(exc),
            )
        finally:
            if session is not None:
                await session.close()

    async def _invoke(self, tool_name: str, arguments: dict[str, object]) -> AdapterResult:
        session = None
        try:
            session = await self.open_session()
            return await session._invoke(tool_name, arguments)
        except MCPClientError as exc:
            return AdapterResult(
                engine=self.spec.engine,
                tool=tool_name,
                ok=False,
                error_code=exc.code,
                message=exc.message,
            )
        except OSError as exc:
            return AdapterResult(
                engine=self.spec.engine,
                tool=tool_name,
                ok=False,
                error_code="MCP_PROCESS_FAILED",
                message=str(exc),
            )
        finally:
            if session is not None:
                await session.close()

    def _adapt_arguments(
        self, canonical_tool: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        if self.spec.engine == "designcraft":
            if canonical_tool == "command_run":
                command = arguments.get("id")
                if not isinstance(command, str) or not command:
                    raise ValueError("DesignCraft command_run requires a normalized 'id' field")
                mapped: dict[str, object] = {"command": command}
                if "params" in arguments:
                    mapped["params"] = arguments["params"]
                return mapped
            if canonical_tool == "command_batch":
                if arguments.get("stop_on_error", True) is not True:
                    raise ValueError("DesignCraft batch cannot continue after a failed command")
                steps = arguments.get("steps")
                if not isinstance(steps, list):
                    raise ValueError("DesignCraft command_batch requires a normalized 'steps' list")
                commands: list[dict[str, object]] = []
                for step in steps:
                    if not isinstance(step, dict) or not isinstance(step.get("id"), str):
                        raise TypeError("Each DesignCraft batch step must include a command 'id'")
                    command: dict[str, object] = {"command": step["id"]}
                    if "params" in step:
                        command["params"] = step["params"]
                    commands.append(command)
                return {"commands": commands}
        if self.spec.engine != "effectcraft":
            return arguments
        if canonical_tool == "command_run":
            command = arguments.get("id")
            if not isinstance(command, str) or not command:
                raise ValueError("EffectCraft command_run requires the normalized 'id' field")
            mapped = {"command": command}
            if "params" in arguments:
                mapped["params"] = arguments["params"]
            return mapped
        if canonical_tool == "command_batch":
            if arguments.get("stop_on_error", True) is not True:
                raise ValueError("EffectCraft batch cannot continue after a failed command")
            steps = arguments.get("steps")
            if not isinstance(steps, list):
                raise ValueError("EffectCraft command_batch requires a normalized 'steps' list")
            mapped_steps = []
            for step in steps:
                if not isinstance(step, dict) or not isinstance(step.get("id"), str):
                    raise TypeError("Each normalized batch step must include a command 'id'")
                mapped_step: dict[str, object] = {"command": step["id"]}
                if "params" in step:
                    mapped_step["params"] = step["params"]
                mapped_steps.append(mapped_step)
            # EffectCraft atomic:false stops on a failure and keeps prior successful steps,
            # which matches the normalized stop_on_error:true behavior.
            return {"steps": mapped_steps, "atomic": False}
        return arguments

    async def inspect(self, request: InspectRequest) -> InspectResult:
        return await self._invoke("doc_inspect", request.arguments)

    async def execute(self, request: ExecuteRequest) -> ExecuteResult:
        return await self._invoke("command_run", request.arguments)

    async def batch(self, request: BatchRequest) -> BatchResult:
        return await self._invoke("command_batch", request.arguments)

    async def preview(self, request: PreviewRequest) -> PreviewResult:
        return await self._invoke("render_preview", request.arguments)
