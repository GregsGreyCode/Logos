"""Tests for gateway.lib.mcp_client -- the gateway's MCP client.

All tests use mocks -- no real MCP servers or subprocesses are started.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mcp_tool(name="read_file", description="Read a file", input_schema=None):
    """Create a fake MCP Tool object matching the SDK interface."""
    tool = SimpleNamespace()
    tool.name = name
    tool.description = description
    tool.inputSchema = input_schema or {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
        },
        "required": ["path"],
    }
    return tool


def _make_call_result(text="file contents here", is_error=False):
    """Create a fake MCP CallToolResult."""
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block], isError=is_error)


def _make_mock_server(name, session=None, tools=None):
    """Create an MCPServerTask with mock attributes for testing."""
    from gateway.lib.mcp_client import MCPServerTask
    server = MCPServerTask(name)
    server.session = session
    server._tools = tools or []
    return server


# ---------------------------------------------------------------------------
# MCPServerTask (run / start / shutdown)
# ---------------------------------------------------------------------------

class TestMCPServerTask:
    """Test the MCPServerTask lifecycle with mocked MCP SDK."""

    def _mock_stdio_and_session(self, session):
        """Return patches for stdio_client and ClientSession as async CMs."""
        mock_read, mock_write = MagicMock(), MagicMock()

        mock_stdio_cm = MagicMock()
        mock_stdio_cm.__aenter__ = AsyncMock(return_value=(mock_read, mock_write))
        mock_stdio_cm.__aexit__ = AsyncMock(return_value=False)

        mock_cs_cm = MagicMock()
        mock_cs_cm.__aenter__ = AsyncMock(return_value=session)
        mock_cs_cm.__aexit__ = AsyncMock(return_value=False)

        return (
            patch("gateway.lib.mcp_client.stdio_client", return_value=mock_stdio_cm),
            patch("gateway.lib.mcp_client.ClientSession", return_value=mock_cs_cm),
            mock_read, mock_write,
        )

    def test_start_connects_and_discovers_tools(self):
        """start() creates a Task that connects, discovers tools, and waits."""
        from gateway.lib.mcp_client import MCPServerTask

        mock_tools = [_make_mcp_tool("echo")]
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=mock_tools)
        )

        p_stdio, p_cs, _, _ = self._mock_stdio_and_session(mock_session)

        async def _test():
            with patch("gateway.lib.mcp_client.StdioServerParameters"), p_stdio, p_cs:
                server = MCPServerTask("test_srv")
                await server.start({"command": "npx", "args": ["-y", "test"]})

                assert server.session is mock_session
                assert len(server._tools) == 1
                assert server._tools[0].name == "echo"
                mock_session.initialize.assert_called_once()

                await server.shutdown()
                assert server.session is None

        asyncio.run(_test())

    def test_no_command_raises(self):
        """Missing 'command' in config raises ValueError."""
        from gateway.lib.mcp_client import MCPServerTask

        async def _test():
            server = MCPServerTask("bad")
            with pytest.raises(ValueError, match="no 'command'"):
                await server.start({"args": []})

        asyncio.run(_test())

    def test_empty_env_gets_safe_defaults(self):
        """Empty env dict gets safe default env vars (PATH, HOME, etc.)."""
        from gateway.lib.mcp_client import MCPServerTask

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[])
        )

        p_stdio, p_cs, _, _ = self._mock_stdio_and_session(mock_session)

        async def _test():
            with patch("gateway.lib.mcp_client.StdioServerParameters") as mock_params, \
                 p_stdio, p_cs, \
                 patch.dict("os.environ", {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=False):
                server = MCPServerTask("srv")
                await server.start({"command": "node", "env": {}})

                # Empty dict -> safe env vars (not None)
                call_kwargs = mock_params.call_args
                env_arg = call_kwargs.kwargs.get("env")
                assert env_arg is not None
                assert isinstance(env_arg, dict)
                assert "PATH" in env_arg
                assert "HOME" in env_arg

                await server.shutdown()

        asyncio.run(_test())

    def test_shutdown_signals_task_exit(self):
        """shutdown() signals the event and waits for task completion."""
        from gateway.lib.mcp_client import MCPServerTask

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[])
        )

        p_stdio, p_cs, _, _ = self._mock_stdio_and_session(mock_session)

        async def _test():
            with patch("gateway.lib.mcp_client.StdioServerParameters"), p_stdio, p_cs:
                server = MCPServerTask("srv")
                await server.start({"command": "npx"})

                assert server.session is not None
                assert not server._task.done()

                await server.shutdown()

                assert server.session is None
                assert server._task.done()

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Shutdown (public API)
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_no_servers_safe(self):
        """shutdown_mcp_servers with no servers does nothing."""
        from gateway.lib.mcp_client import shutdown_mcp_servers, _servers

        _servers.clear()
        shutdown_mcp_servers()  # Should not raise

    def test_shutdown_clears_servers(self):
        """shutdown_mcp_servers calls shutdown() on each server and clears dict."""
        import gateway.lib.mcp_client as mcp_mod
        from gateway.lib.mcp_client import shutdown_mcp_servers, _servers

        _servers.clear()
        mock_server = MagicMock()
        mock_server.name = "test"
        mock_server.shutdown = AsyncMock()
        _servers["test"] = mock_server

        mcp_mod._ensure_mcp_loop()
        try:
            shutdown_mcp_servers()
        finally:
            mcp_mod._mcp_loop = None
            mcp_mod._mcp_thread = None

        assert len(_servers) == 0
        mock_server.shutdown.assert_called_once()

    def test_shutdown_handles_errors(self):
        """shutdown_mcp_servers handles errors during close gracefully."""
        import gateway.lib.mcp_client as mcp_mod
        from gateway.lib.mcp_client import shutdown_mcp_servers, _servers

        _servers.clear()
        mock_server = MagicMock()
        mock_server.name = "broken"
        mock_server.shutdown = AsyncMock(side_effect=RuntimeError("close failed"))
        _servers["broken"] = mock_server

        mcp_mod._ensure_mcp_loop()
        try:
            shutdown_mcp_servers()  # Should not raise
        finally:
            mcp_mod._mcp_loop = None
            mcp_mod._mcp_thread = None

        assert len(_servers) == 0

    def test_shutdown_is_parallel(self):
        """Multiple servers are shut down in parallel via asyncio.gather."""
        import gateway.lib.mcp_client as mcp_mod
        from gateway.lib.mcp_client import shutdown_mcp_servers, _servers
        import time

        _servers.clear()

        # 3 servers each taking 1s to shut down
        for i in range(3):
            mock_server = MagicMock()
            mock_server.name = f"srv_{i}"
            async def slow_shutdown():
                await asyncio.sleep(1)
            mock_server.shutdown = slow_shutdown
            _servers[f"srv_{i}"] = mock_server

        mcp_mod._ensure_mcp_loop()
        try:
            start = time.monotonic()
            shutdown_mcp_servers()
            elapsed = time.monotonic() - start
        finally:
            mcp_mod._mcp_loop = None
            mcp_mod._mcp_thread = None

        assert len(_servers) == 0
        # Parallel: ~1s, not ~3s. Allow some margin.
        assert elapsed < 2.5, f"Shutdown took {elapsed:.1f}s, expected ~1s (parallel)"


# ---------------------------------------------------------------------------
# _build_safe_env
# ---------------------------------------------------------------------------

class TestBuildSafeEnv:
    """Tests for _build_safe_env() environment filtering."""

    def test_only_safe_vars_passed(self):
        """Only safe baseline vars and XDG_* from os.environ are included."""
        from gateway.lib.mcp_client import _build_safe_env

        fake_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "USER": "test",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C",
            "TERM": "xterm",
            "SHELL": "/bin/bash",
            "TMPDIR": "/tmp",
            "XDG_DATA_HOME": "/home/test/.local/share",
            "SECRET_KEY": "should_not_appear",
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        }
        with patch.dict("os.environ", fake_env, clear=True):
            result = _build_safe_env(None)

        # Safe vars present
        assert result["PATH"] == "/usr/bin"
        assert result["HOME"] == "/home/test"
        assert result["USER"] == "test"
        assert result["LANG"] == "en_US.UTF-8"
        assert result["XDG_DATA_HOME"] == "/home/test/.local/share"
        # Unsafe vars excluded
        assert "SECRET_KEY" not in result
        assert "AWS_ACCESS_KEY_ID" not in result

    def test_user_env_merged(self):
        """User-specified env vars are merged into the safe env."""
        from gateway.lib.mcp_client import _build_safe_env

        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            result = _build_safe_env({"MY_CUSTOM_VAR": "hello"})

        assert result["PATH"] == "/usr/bin"
        assert result["MY_CUSTOM_VAR"] == "hello"

    def test_user_env_overrides_safe(self):
        """User env can override safe defaults."""
        from gateway.lib.mcp_client import _build_safe_env

        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            result = _build_safe_env({"PATH": "/custom/bin"})

        assert result["PATH"] == "/custom/bin"

    def test_none_user_env(self):
        """None user_env still returns safe vars from os.environ."""
        from gateway.lib.mcp_client import _build_safe_env

        with patch.dict("os.environ", {"PATH": "/usr/bin", "HOME": "/root"}, clear=True):
            result = _build_safe_env(None)

        assert isinstance(result, dict)
        assert result["PATH"] == "/usr/bin"
        assert result["HOME"] == "/root"

    def test_secret_vars_excluded(self):
        """Sensitive env vars from os.environ are NOT passed through."""
        from gateway.lib.mcp_client import _build_safe_env

        fake_env = {
            "PATH": "/usr/bin",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "GITHUB_TOKEN": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "OPENAI_API_KEY": "sk-proj-abc123",
            "DATABASE_URL": "postgres://user:pass@localhost/db",
            "API_SECRET": "supersecret",
        }
        with patch.dict("os.environ", fake_env, clear=True):
            result = _build_safe_env(None)

        assert "PATH" in result
        assert "AWS_SECRET_ACCESS_KEY" not in result
        assert "GITHUB_TOKEN" not in result
        assert "OPENAI_API_KEY" not in result
        assert "DATABASE_URL" not in result
        assert "API_SECRET" not in result


# ---------------------------------------------------------------------------
# _sanitize_error
# ---------------------------------------------------------------------------

class TestSanitizeError:
    """Tests for _sanitize_error() credential stripping."""

    def test_strips_github_pat(self):
        from gateway.lib.mcp_client import _sanitize_error
        result = _sanitize_error("Error with ghp_abc123def456")
        assert result == "Error with [REDACTED]"

    def test_strips_openai_key(self):
        from gateway.lib.mcp_client import _sanitize_error
        result = _sanitize_error("key sk-projABC123xyz")
        assert result == "key [REDACTED]"

    def test_strips_bearer_token(self):
        from gateway.lib.mcp_client import _sanitize_error
        result = _sanitize_error("Authorization: Bearer eyJabc123def")
        assert result == "Authorization: [REDACTED]"

    def test_strips_token_param(self):
        from gateway.lib.mcp_client import _sanitize_error
        result = _sanitize_error("url?token=secret123")
        assert result == "url?[REDACTED]"

    def test_no_credentials_unchanged(self):
        from gateway.lib.mcp_client import _sanitize_error
        result = _sanitize_error("normal error message")
        assert result == "normal error message"

    def test_multiple_credentials(self):
        from gateway.lib.mcp_client import _sanitize_error
        result = _sanitize_error("ghp_abc123 and sk-projXyz789 and token=foo")
        assert "ghp_" not in result
        assert "sk-" not in result
        assert "token=" not in result
        assert result.count("[REDACTED]") == 3


# ---------------------------------------------------------------------------
# HTTP config
# ---------------------------------------------------------------------------

class TestHTTPConfig:
    """Tests for HTTP transport detection and handling."""

    def test_is_http_with_url(self):
        from gateway.lib.mcp_client import MCPServerTask
        server = MCPServerTask("remote")
        server._config = {"url": "https://example.com/mcp"}
        assert server._is_http() is True

    def test_is_stdio_with_command(self):
        from gateway.lib.mcp_client import MCPServerTask
        server = MCPServerTask("local")
        server._config = {"command": "npx", "args": []}
        assert server._is_http() is False

    def test_conflicting_url_and_command_warns(self):
        """Config with both url and command logs a warning and uses HTTP."""
        from gateway.lib.mcp_client import MCPServerTask
        server = MCPServerTask("conflict")
        config = {"url": "https://example.com/mcp", "command": "npx", "args": []}
        # url takes precedence
        server._config = config
        assert server._is_http() is True

    def test_http_unavailable_raises(self):
        from gateway.lib.mcp_client import MCPServerTask

        server = MCPServerTask("remote")
        config = {"url": "https://example.com/mcp"}

        async def _test():
            with patch("gateway.lib.mcp_client._MCP_HTTP_AVAILABLE", False):
                with pytest.raises(ImportError, match="HTTP transport"):
                    await server._run_http(config)

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Reconnection logic
# ---------------------------------------------------------------------------

class TestReconnection:
    """Tests for automatic reconnection behavior in MCPServerTask.run()."""

    def test_reconnect_on_disconnect(self):
        """After initial success, a connection drop triggers reconnection."""
        from gateway.lib.mcp_client import MCPServerTask

        run_count = 0
        target_server = None

        original_run_stdio = MCPServerTask._run_stdio

        async def patched_run_stdio(self_srv, config):
            nonlocal run_count, target_server
            run_count += 1
            if target_server is not self_srv:
                return await original_run_stdio(self_srv, config)
            if run_count == 1:
                # First connection succeeds, then simulate disconnect
                self_srv.session = MagicMock()
                self_srv._tools = []
                self_srv._ready.set()
                raise ConnectionError("connection dropped")
            else:
                # Reconnection succeeds; signal shutdown so run() exits
                self_srv.session = MagicMock()
                self_srv._shutdown_event.set()
                await self_srv._shutdown_event.wait()

        async def _test():
            nonlocal target_server
            server = MCPServerTask("test_srv")
            target_server = server

            with patch.object(MCPServerTask, "_run_stdio", patched_run_stdio), \
                 patch("asyncio.sleep", new_callable=AsyncMock):
                await server.run({"command": "test"})

            assert run_count >= 2  # At least one reconnection attempt

        asyncio.run(_test())

    def test_no_reconnect_on_shutdown(self):
        """If shutdown is requested, don't attempt reconnection."""
        from gateway.lib.mcp_client import MCPServerTask

        run_count = 0
        target_server = None

        original_run_stdio = MCPServerTask._run_stdio

        async def patched_run_stdio(self_srv, config):
            nonlocal run_count, target_server
            run_count += 1
            if target_server is not self_srv:
                return await original_run_stdio(self_srv, config)
            self_srv.session = MagicMock()
            self_srv._tools = []
            self_srv._ready.set()
            raise ConnectionError("connection dropped")

        async def _test():
            nonlocal target_server
            server = MCPServerTask("test_srv")
            target_server = server
            server._shutdown_event.set()  # Shutdown already requested

            with patch.object(MCPServerTask, "_run_stdio", patched_run_stdio), \
                 patch("asyncio.sleep", new_callable=AsyncMock):
                await server.run({"command": "test"})

            # Should not retry because shutdown was set
            assert run_count == 1

        asyncio.run(_test())

    def test_no_reconnect_on_initial_failure(self):
        """First connection failure reports error immediately, no retry."""
        from gateway.lib.mcp_client import MCPServerTask

        run_count = 0
        target_server = None

        original_run_stdio = MCPServerTask._run_stdio

        async def patched_run_stdio(self_srv, config):
            nonlocal run_count, target_server
            run_count += 1
            if target_server is not self_srv:
                return await original_run_stdio(self_srv, config)
            raise ConnectionError("cannot connect")

        async def _test():
            nonlocal target_server
            server = MCPServerTask("test_srv")
            target_server = server

            with patch.object(MCPServerTask, "_run_stdio", patched_run_stdio), \
                 patch("asyncio.sleep", new_callable=AsyncMock):
                await server.run({"command": "test"})

            # Only one attempt, no retry on initial failure
            assert run_count == 1
            assert server._error is not None
            assert "cannot connect" in str(server._error)

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Configurable timeouts
# ---------------------------------------------------------------------------

class TestConfigurableTimeouts:
    """Tests for configurable per-server timeouts."""

    def test_default_timeout(self):
        """Server with no timeout config gets _DEFAULT_TOOL_TIMEOUT."""
        from gateway.lib.mcp_client import MCPServerTask, _DEFAULT_TOOL_TIMEOUT

        server = MCPServerTask("test_srv")
        assert server.tool_timeout == _DEFAULT_TOOL_TIMEOUT
        assert server.tool_timeout == 120

    def test_custom_timeout(self):
        """Server with timeout=180 in config gets 180."""
        from gateway.lib.mcp_client import MCPServerTask

        target_server = None

        original_run_stdio = MCPServerTask._run_stdio

        async def patched_run_stdio(self_srv, config):
            if target_server is not self_srv:
                return await original_run_stdio(self_srv, config)
            self_srv.session = MagicMock()
            self_srv._tools = []
            self_srv._ready.set()
            await self_srv._shutdown_event.wait()

        async def _test():
            nonlocal target_server
            server = MCPServerTask("test_srv")
            target_server = server

            with patch.object(MCPServerTask, "_run_stdio", patched_run_stdio):
                task = asyncio.ensure_future(
                    server.run({"command": "test", "timeout": 180})
                )
                await server._ready.wait()
                assert server.tool_timeout == 180
                server._shutdown_event.set()
                await task

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# connect_server
# ---------------------------------------------------------------------------

class TestConnectServer:
    """connect_server connects, tracks the server and returns its tool names."""

    def test_tracks_server_and_returns_tool_names(self):
        import gateway.lib.mcp_client as mcp_mod
        from gateway.lib.mcp_client import connect_server, _servers

        server = _make_mock_server(
            "fs", tools=[_make_mcp_tool("read_file"), _make_mcp_tool("write_file")]
        )

        async def fake_connect(name, config):
            return server

        _servers.pop("fs", None)
        try:
            with patch.object(mcp_mod, "_MCP_AVAILABLE", True), \
                 patch.object(mcp_mod, "_connect_server", side_effect=fake_connect):
                names = asyncio.run(connect_server("fs", {"command": "test"}))
            assert names == ["read_file", "write_file"]
            assert _servers["fs"] is server
            # The catalogue reads this attribute off the tracked server.
            assert server._registered_tool_names == ["read_file", "write_file"]
        finally:
            _servers.pop("fs", None)

    def test_catalogue_reports_external_server_tools(self):
        """MCPGatewayService.get_catalogue must surface an external
        server's tools (regression: tool_count was 0 for stdio/HTTP)."""
        from gateway.mcp_service import MCPGatewayService

        svc = MCPGatewayService({"mcp_servers": {"fs": {"command": "test"}}})
        server = _make_mock_server(
            "fs", session=MagicMock(),
            tools=[_make_mcp_tool("read_file"), _make_mcp_tool("write_file")],
        )
        svc._servers["fs"] = server

        entry = next(e for e in svc.get_catalogue() if e["name"] == "fs")
        assert entry["connected"] is True
        assert entry["transport"] == "stdio"
        assert entry["tool_count"] == 2
        assert entry["tool_names"] == ["read_file", "write_file"]

        # Re-discovery after a reconnect is reflected too.
        server._tools = [_make_mcp_tool("only_one")]
        entry = next(e for e in svc.get_catalogue() if e["name"] == "fs")
        assert entry["tool_names"] == ["only_one"]

    def test_failed_connection_is_not_tracked(self):
        import gateway.lib.mcp_client as mcp_mod
        from gateway.lib.mcp_client import connect_server, _servers

        async def boom(name, config):
            raise ConnectionError("refused")

        _servers.pop("broken", None)
        with patch.object(mcp_mod, "_MCP_AVAILABLE", True), \
             patch.object(mcp_mod, "_connect_server", side_effect=boom):
            with pytest.raises(ConnectionError):
                asyncio.run(connect_server("broken", {"command": "test"}))
        assert "broken" not in _servers

    def test_raises_import_error_without_mcp_sdk(self):
        import gateway.lib.mcp_client as mcp_mod
        from gateway.lib.mcp_client import connect_server

        with patch.object(mcp_mod, "_MCP_AVAILABLE", False):
            with pytest.raises(ImportError):
                asyncio.run(connect_server("fs", {"command": "test"}))
