"""Lifecycle-safe MCP client for local stdio child processes."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, ListToolsResult


class MCPClientError(Exception):
    """Structured failure from child initialization or a timed MCP request."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class StdioMCPClient:
    """Small typed wrapper over an initialized MCP ClientSession."""

    def __init__(self, session: ClientSession, timeout_seconds: float, close_stack) -> None:
        self._session = session
        self._timeout_seconds = timeout_seconds
        self._close_stack = close_stack
        self._closed = False
        self.initialized = True

    async def _request(self, operation: str, request) -> object:
        if self._closed:
            raise MCPClientError("CLIENT_CLOSED", "The child MCP client has been closed")
        try:
            return await asyncio.wait_for(request, timeout=self._timeout_seconds)
        except TimeoutError as exc:
            self._closed = True
            await self._close_stack()
            raise MCPClientError(
                "MCP_TIMEOUT",
                f"MCP call to '{operation}' exceeded {self._timeout_seconds:g} seconds",
            ) from exc

    async def list_tools(self) -> ListToolsResult:
        return await self._request("tools/list", self._session.list_tools())

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        return await self._request(name, self._session.call_tool(name, arguments))


@asynccontextmanager
async def open_stdio_mcp(
    command: Path,
    args: list[str],
    cwd: Path | None = None,
    *,
    env_overrides: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
    initialization_timeout_seconds: float = 30.0,
) -> AsyncIterator[StdioMCPClient]:
    """Start, initialize, and manage one MCP child using the official SDK transport."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if initialization_timeout_seconds <= 0:
        raise ValueError("initialization_timeout_seconds must be positive")
    command_path = command.expanduser().absolute()
    working_directory = cwd.expanduser().resolve() if cwd is not None else None
    child_environment = None
    if env_overrides:
        child_environment = {**os.environ, **env_overrides}
    parameters = StdioServerParameters(
        command=str(command_path),
        args=list(args),
        cwd=str(working_directory) if working_directory else None,
        env=child_environment,
    )
    stack = AsyncExitStack()
    try:
        read_stream, write_stream = await stack.enter_async_context(stdio_client(parameters))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        try:
            await asyncio.wait_for(
                session.initialize(), timeout=initialization_timeout_seconds
            )
        except TimeoutError as exc:
            await stack.aclose()
            raise MCPClientError(
                "MCP_INITIALIZATION_TIMEOUT",
                f"MCP initialization exceeded {initialization_timeout_seconds:g} seconds",
            ) from exc
        except Exception as exc:
            await stack.aclose()
            raise MCPClientError("MCP_INITIALIZATION_FAILED", str(exc)) from exc
        yield StdioMCPClient(session, timeout_seconds, stack.aclose)
    finally:
        await stack.aclose()
