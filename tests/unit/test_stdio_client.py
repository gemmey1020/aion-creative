import asyncio
import os
import sys
from pathlib import Path

import pytest


def _load_stdio_client(project_root: Path):
    module_path = project_root / "src" / "ion_creative" / "mcp" / "stdio_client.py"
    assert module_path.is_file(), "Ion child-MCP client is not implemented"
    from ion_creative.mcp.stdio_client import MCPClientError, open_stdio_mcp

    return MCPClientError, open_stdio_mcp


def _fake_child(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "fake_craft_server.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "\n".join(
            (
                "import asyncio",
                "import os",
                "from pathlib import Path",
                "from mcp.server import MCPServer",
                "server = MCPServer('ion-test-child')",
                f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')",
                "@server.tool()",
                "async def echo(message: str) -> str:",
                "    return message",
                "@server.tool()",
                "async def wait(seconds: float) -> str:",
                "    await asyncio.sleep(seconds)",
                "    return 'finished'",
                "@server.tool()",
                "async def env_value(name: str) -> str:",
                "    return os.environ.get(name, '')",
                "server.run(transport='stdio')",
                "",
            )
        ),
        encoding="utf-8",
    )
    return script, pid_file


async def _assert_process_exited(pid: int) -> None:
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"MCP child process {pid} did not terminate")


def test_stdio_client_initializes_lists_calls_and_shuts_down(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    _, open_stdio_mcp = _load_stdio_client(project_root)
    script, pid_file = _fake_child(tmp_path)

    async def exercise() -> int:
        async with open_stdio_mcp(
            Path(sys.executable),
            ["-u", str(script)],
            cwd=tmp_path,
            env_overrides={"ION_TEST_HOME": str(tmp_path / "isolated-home")},
        ) as client:
            assert client.initialized is True
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {"echo", "wait", "env_value"}
            result = await client.call_tool("echo", {"message": "mcp-smoke"})
            assert result.is_error is False
            assert "mcp-smoke" in result.content[0].text
            environment = await client.call_tool("env_value", {"name": "ION_TEST_HOME"})
            assert str(tmp_path / "isolated-home") in environment.content[0].text
        return int(pid_file.read_text(encoding="utf-8"))

    pid = asyncio.run(exercise())
    asyncio.run(_assert_process_exited(pid))


def test_stdio_client_timeout_is_structured_and_terminates_child(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    MCPClientError, open_stdio_mcp = _load_stdio_client(project_root)
    script, pid_file = _fake_child(tmp_path)

    async def exercise() -> int:
        async with open_stdio_mcp(
            Path(sys.executable),
            ["-u", str(script)],
            cwd=tmp_path,
            timeout_seconds=0.2,
            initialization_timeout_seconds=10.0,
        ) as client:
            pid = int(pid_file.read_text(encoding="utf-8"))
            with pytest.raises(MCPClientError) as caught:
                await client.call_tool("wait", {"seconds": 30})
            assert caught.value.code == "MCP_TIMEOUT"
            assert caught.value.to_dict() == {
                "code": "MCP_TIMEOUT",
                "message": "MCP call to 'wait' exceeded 0.2 seconds",
            }
            await _assert_process_exited(pid)
            return pid

    asyncio.run(exercise())
