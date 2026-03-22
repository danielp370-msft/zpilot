"""Comprehensive resilience tests for transport retry, circuit breaker, SSH retry,
proxy error handling, and health tracker edge cases."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zpilot.transport import CircuitBreaker, ExecResult, MCPTransport, SSHTransport


# ── Test MCPTransport retry logic ──────────────────────────────

class TestMCPRetryLogic:
    """Test MCPTransport retry behavior on network failures."""

    def _make_transport(self, **overrides) -> MCPTransport:
        defaults = dict(
            url="https://fakehost:8222",
            max_retries=3,
            retry_delay=0.001,
            circuit_failure_threshold=100,  # high so circuit doesn't interfere
        )
        defaults.update(overrides)
        return MCPTransport(**defaults)

    @pytest.mark.asyncio
    async def test_exec_retries_on_timeout_then_succeeds(self):
        """exec retries on timeout, succeeds on 2nd attempt."""
        import httpx

        t = self._make_transport()
        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.raise_for_status = MagicMock()
        good_resp.json.return_value = {"returncode": 0, "stdout": "ok", "stderr": ""}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[httpx.TimeoutException("timed out"), good_resp]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hi")
        assert result.ok
        assert result.stdout == "ok"

    @pytest.mark.asyncio
    async def test_exec_retries_on_connect_error_then_succeeds(self):
        """exec retries on connection error, succeeds on 3rd attempt."""
        import httpx

        t = self._make_transport()
        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.raise_for_status = MagicMock()
        good_resp.json.return_value = {"returncode": 0, "stdout": "ok", "stderr": ""}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused again"),
            good_resp,
        ])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hi")
        assert result.ok
        assert result.stdout == "ok"

    @pytest.mark.asyncio
    async def test_exec_exhausts_all_retries(self):
        """exec returns error ExecResult when all retries exhausted."""
        import httpx

        t = self._make_transport(max_retries=2)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hi")
        assert not result.ok
        assert "after 2 attempts" in result.stderr

    @pytest.mark.asyncio
    async def test_upload_retries_on_timeout(self):
        """upload retries on timeout, succeeds on 2nd attempt."""
        import httpx

        t = self._make_transport()
        good_resp = MagicMock()
        good_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[httpx.TimeoutException("timeout"), good_resp]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("builtins.open", MagicMock(return_value=MagicMock(
                 __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b"data"))),
                 __exit__=MagicMock(return_value=False),
             ))):
            await t.upload("/tmp/test.txt", "/remote/test.txt")
        # If no exception, upload succeeded

    @pytest.mark.asyncio
    async def test_download_retries_on_connection_reset(self):
        """download retries on protocol error, succeeds on 2nd attempt."""
        import httpx
        import base64

        t = self._make_transport()
        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.json.return_value = {"content": base64.b64encode(b"hello").decode()}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[httpx.RemoteProtocolError("reset"), good_resp]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_file = MagicMock()
        mock_open = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=mock_file),
            __exit__=MagicMock(return_value=False),
        ))

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("builtins.open", mock_open):
            await t.download("/remote/test.txt", "/tmp/test.txt")
        mock_file.write.assert_called_once_with(b"hello")

    @pytest.mark.asyncio
    async def test_upload_exhausts_retries_raises_ioerror(self):
        """upload raises IOError when all retries are exhausted."""
        import httpx

        t = self._make_transport(max_retries=2)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("builtins.open", MagicMock(return_value=MagicMock(
                 __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b"data"))),
                 __exit__=MagicMock(return_value=False),
             ))):
            with pytest.raises(IOError, match="after 2 attempts"):
                await t.upload("/tmp/test.txt", "/remote/test.txt")

    @pytest.mark.asyncio
    async def test_download_exhausts_retries_raises_ioerror(self):
        """download raises IOError when all retries are exhausted."""
        import httpx

        t = self._make_transport(max_retries=2)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(IOError, match="after 2 attempts"):
                await t.download("/remote/test.txt", "/tmp/test.txt")

    @pytest.mark.asyncio
    async def test_exponential_backoff_timing(self):
        """Verify exponential backoff delays are correct."""
        import httpx

        t = self._make_transport(max_retries=3, retry_delay=1.0)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        sleep_mock = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", sleep_mock):
            await t.exec("echo hi")

        # 3 retries → 2 sleeps: delay*2^0=1.0, delay*2^1=2.0
        assert sleep_mock.call_count == 2
        assert sleep_mock.call_args_list[0][0][0] == 1.0
        assert sleep_mock.call_args_list[1][0][0] == 2.0

    @pytest.mark.asyncio
    async def test_401_auth_failure_does_not_retry(self):
        """401 auth failure should NOT retry — returned immediately."""
        t = self._make_transport(max_retries=3)
        resp_401 = MagicMock()
        resp_401.status_code = 401

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp_401)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        sleep_mock = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", sleep_mock):
            result = await t.exec("echo hi")

        assert not result.ok
        assert "401" in result.stderr
        # Should only be called once — no retries for auth failure
        assert mock_client.post.call_count == 1
        assert sleep_mock.call_count == 0

    @pytest.mark.asyncio
    async def test_500_server_error_does_retry(self):
        """500 server errors should retry (raise_for_status triggers retry)."""
        import httpx

        t = self._make_transport(max_retries=2)

        # First call: 500 (raise_for_status raises), second: 200
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=resp_500)
        )

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.raise_for_status = MagicMock()
        resp_200.json.return_value = {"returncode": 0, "stdout": "ok", "stderr": ""}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[resp_500, resp_200])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hi")
        assert result.ok
        assert mock_client.post.call_count == 2


# ── Test CircuitBreaker ────────────────────────────────────────

class TestCircuitBreaker:
    """Test circuit breaker state machine."""

    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_stays_closed_under_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_opens_after_failure_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_open is True

    def test_rejects_requests_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=999)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.allow_request() is False

    def test_transitions_to_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

        # Simulate time passing
        cb.last_failure_time = time.monotonic() - 1.0
        assert cb.allow_request() is True
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_half_open_to_closed_on_success(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        cb.last_failure_time = time.monotonic() - 1.0
        cb.allow_request()  # transitions to HALF_OPEN
        assert cb.state == CircuitBreaker.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.failure_count == 0

    def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        cb.last_failure_time = time.monotonic() - 1.0
        cb.allow_request()  # transitions to HALF_OPEN
        assert cb.state == CircuitBreaker.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

    def test_record_success_resets_count(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 3

        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitBreaker.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_integration_with_mcp(self):
        """Circuit breaker short-circuits exec when open."""
        t = MCPTransport(
            url="https://fakehost:8222",
            max_retries=1,
            retry_delay=0.001,
            circuit_failure_threshold=1,
            circuit_recovery_timeout=999,
        )
        # Force circuit open
        t._circuit.record_failure()
        assert t._circuit.is_open

        # exec should return error immediately without network call
        result = await t.exec("echo hi")
        assert not result.ok
        assert "circuit breaker" in result.stderr

    @pytest.mark.asyncio
    async def test_circuit_breaker_rejects_upload_when_open(self):
        """Circuit breaker rejects upload when open."""
        t = MCPTransport(
            url="https://fakehost:8222",
            max_retries=1,
            retry_delay=0.001,
            circuit_failure_threshold=1,
            circuit_recovery_timeout=999,
        )
        t._circuit.record_failure()

        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b"data"))),
            __exit__=MagicMock(return_value=False),
        ))):
            with pytest.raises(IOError, match="circuit breaker"):
                await t.upload("/tmp/test.txt", "/remote/test.txt")

    @pytest.mark.asyncio
    async def test_circuit_breaker_rejects_download_when_open(self):
        """Circuit breaker rejects download when open."""
        t = MCPTransport(
            url="https://fakehost:8222",
            max_retries=1,
            retry_delay=0.001,
            circuit_failure_threshold=1,
            circuit_recovery_timeout=999,
        )
        t._circuit.record_failure()

        with pytest.raises(IOError, match="circuit breaker"):
            await t.download("/remote/test.txt", "/tmp/test.txt")


# ── Test SSHTransport retry ────────────────────────────────────

class TestSSHRetry:
    """Test SSHTransport retry on transient failures."""

    @pytest.mark.asyncio
    async def test_retry_on_exit_code_255(self):
        """Retry on exit code 255 (SSH connection failed), succeed on 2nd."""
        t = SSHTransport(host="fakehost")

        call_count = 0
        async def mock_exec_once(cmd, timeout=30.0, force_pty=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ExecResult(255, "", "ssh: connect to host fakehost: Connection refused")
            return ExecResult(0, "hello", "")

        with patch.object(t, "_exec_once", side_effect=mock_exec_once), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hello")
        assert result.ok
        assert result.stdout == "hello"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_exit_code_1(self):
        """No retry on exit code 1 (command failed, SSH was fine)."""
        t = SSHTransport(host="fakehost")

        call_count = 0
        async def mock_exec_once(cmd, timeout=30.0, force_pty=False):
            nonlocal call_count
            call_count += 1
            return ExecResult(1, "", "command not found")

        with patch.object(t, "_exec_once", side_effect=mock_exec_once):
            result = await t.exec("badcommand")
        assert not result.ok
        assert call_count == 1  # no retry

    @pytest.mark.asyncio
    async def test_retry_on_connection_refused_in_stderr(self):
        """Retry on 'Connection refused' in stderr."""
        t = SSHTransport(host="fakehost")

        call_count = 0
        async def mock_exec_once(cmd, timeout=30.0, force_pty=False):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return ExecResult(1, "", "Connection refused")
            return ExecResult(0, "success", "")

        with patch.object(t, "_exec_once", side_effect=mock_exec_once), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hi")
        assert result.ok
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_connection_reset_in_stderr(self):
        """Retry on 'Connection reset' in stderr."""
        t = SSHTransport(host="fakehost")

        call_count = 0
        async def mock_exec_once(cmd, timeout=30.0, force_pty=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ExecResult(255, "", "Connection reset by peer")
            return ExecResult(0, "ok", "")

        with patch.object(t, "_exec_once", side_effect=mock_exec_once), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hi")
        assert result.ok

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self):
        """All SSH retries exhausted, returns last failure."""
        t = SSHTransport(host="fakehost")

        async def mock_exec_once(cmd, timeout=30.0, force_pty=False):
            return ExecResult(255, "", "Connection refused")

        with patch.object(t, "_exec_once", side_effect=mock_exec_once), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await t.exec("echo hi")
        assert not result.ok
        assert result.returncode == 255

    @pytest.mark.asyncio
    async def test_no_retry_on_connection_timed_out_with_exit_0(self):
        """If SSH succeeded (exit 0) but stderr has 'Connection timed out', no retry."""
        t = SSHTransport(host="fakehost")

        call_count = 0
        async def mock_exec_once(cmd, timeout=30.0, force_pty=False):
            nonlocal call_count
            call_count += 1
            return ExecResult(0, "ok", "Connection timed out warning")

        with patch.object(t, "_exec_once", side_effect=mock_exec_once):
            result = await t.exec("echo hi")
        assert result.ok
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_is_transient_ssh_failure(self):
        """Unit test the transient failure detection."""
        t = SSHTransport(host="fakehost")
        assert t._is_transient_ssh_failure(ExecResult(255, "", "")) is True
        assert t._is_transient_ssh_failure(ExecResult(1, "", "Connection refused")) is True
        assert t._is_transient_ssh_failure(ExecResult(1, "", "Connection reset")) is True
        assert t._is_transient_ssh_failure(ExecResult(1, "", "Connection timed out")) is True
        assert t._is_transient_ssh_failure(ExecResult(1, "", "No route to host")) is True
        assert t._is_transient_ssh_failure(ExecResult(0, "ok", "")) is False
        assert t._is_transient_ssh_failure(ExecResult(1, "", "file not found")) is False
        assert t._is_transient_ssh_failure(ExecResult(127, "", "command not found")) is False


# ── Test proxy resilience ──────────────────────────────────────

class TestProxyResilience:
    """Test proxy endpoint handles unreachable nodes."""

    @pytest.fixture
    def app(self):
        from zpilot.mcp_http import create_http_app
        from zpilot.models import ZpilotConfig
        config = ZpilotConfig(http_token="test-resilience-token")
        return create_http_app(config)

    @pytest.fixture
    def auth_headers(self):
        return {"Authorization": "Bearer test-resilience-token"}

    @pytest.mark.asyncio
    async def test_proxy_returns_503_when_node_unreachable(self, app, auth_headers):
        """Proxy returns 503 when the target node is unreachable."""
        import httpx
        from zpilot.mcp_http import proxy_to_node

        with patch("zpilot.mcp_http.proxy_to_node", return_value={
            "error": "Node 'deadnode' is unreachable: Connection refused",
            "unreachable": True,
        }):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/proxy/deadnode",
                    json={"tool": "ping", "arguments": {}},
                    headers=auth_headers,
                )
            assert resp.status_code == 503
            assert "unreachable" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_proxy_returns_502_for_non_unreachable_error(self, app, auth_headers):
        """Proxy returns 502 for non-unreachable errors (e.g., unknown node)."""
        import httpx

        with patch("zpilot.mcp_http.proxy_to_node", return_value={
            "error": "Node 'unknown' not found in registry",
        }):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/proxy/unknown",
                    json={"tool": "ping", "arguments": {}},
                    headers=auth_headers,
                )
            assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_proxy_error_message_includes_node_name(self, app, auth_headers):
        """Error message includes the node name for debugging."""
        import httpx

        with patch("zpilot.mcp_http.proxy_to_node", return_value={
            "error": "Node 'mynode' is unreachable: Connection refused",
            "unreachable": True,
        }):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/proxy/mynode",
                    json={"tool": "ping", "arguments": {}},
                    headers=auth_headers,
                )
            assert "mynode" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_proxy_to_node_timeout_returns_unreachable(self):
        """proxy_to_node returns unreachable on timeout."""
        import httpx
        from zpilot.mcp_http import proxy_to_node
        from zpilot.nodes import Node, NodeRegistry
        from zpilot.transport import MCPTransport

        node = Node(
            name="timeout-node",
            transport_type="mcp",
            host="https://fakehost:8222",
            transport_opts={"token": "x", "max_retries": 1, "retry_delay": 0.001},
        )

        with patch("zpilot.mcp_http.load_nodes", return_value=[
            Node(name="local"),
            node,
        ]):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await proxy_to_node("timeout-node", "ping", {})
        assert "error" in result
        assert result.get("unreachable") is True
        assert "timeout-node" in result["error"]

    @pytest.mark.asyncio
    async def test_proxy_to_node_connect_error_returns_unreachable(self):
        """proxy_to_node returns unreachable on connection error."""
        import httpx
        from zpilot.mcp_http import proxy_to_node
        from zpilot.nodes import Node

        node = Node(
            name="dead-node",
            transport_type="mcp",
            host="https://fakehost:8222",
            transport_opts={"token": "x", "max_retries": 1, "retry_delay": 0.001},
        )

        with patch("zpilot.mcp_http.load_nodes", return_value=[
            Node(name="local"),
            node,
        ]):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await proxy_to_node("dead-node", "ping", {})
        assert result.get("unreachable") is True
        assert "dead-node" in result["error"]


# ── Test health tracker resilience ─────────────────────────────

class TestHealthTrackerResilience:
    """Test health tracker handles flapping nodes."""

    def _make_node(self, name, alive=True):
        """Create a node with a controllable transport."""
        from zpilot.nodes import Node
        from zpilot.transport import LocalTransport

        class ControllableTransport(LocalTransport):
            def __init__(self, alive_flag=True):
                self.alive_flag = alive_flag

            async def is_alive(self):
                if not self.alive_flag:
                    raise ConnectionError("node down")
                return True

        node = Node(name=name, transport_type="ssh", host="fake")
        node._transport = ControllableTransport(alive)
        return node

    @pytest.mark.asyncio
    async def test_node_goes_offline_after_consecutive_failures(self):
        """Node transitions to offline after offline_threshold failures."""
        from zpilot.monitor import NodeHealthTracker
        from zpilot.nodes import NodeRegistry

        node = self._make_node("flaky", alive=False)
        registry = NodeRegistry([node])
        tracker = NodeHealthTracker(registry, offline_threshold=3, degraded_threshold=1)

        # 1st failure → degraded
        await tracker.check_node(node)
        assert tracker.get_health("flaky")["state"] == "degraded"

        # 2nd failure → still degraded
        await tracker.check_node(node)
        assert tracker.get_health("flaky")["state"] == "degraded"

        # 3rd failure → offline
        await tracker.check_node(node)
        assert tracker.get_health("flaky")["state"] == "offline"

    @pytest.mark.asyncio
    async def test_node_recovers_after_coming_back_online(self):
        """Node transitions from offline back to online."""
        from zpilot.monitor import NodeHealthTracker
        from zpilot.nodes import NodeRegistry

        node = self._make_node("recover", alive=False)
        registry = NodeRegistry([node])
        tracker = NodeHealthTracker(registry, offline_threshold=1)

        # Go offline
        await tracker.check_node(node)
        assert tracker.get_health("recover")["state"] == "offline"

        # Come back
        node._transport.alive_flag = True
        await tracker.check_node(node)
        assert tracker.get_health("recover")["state"] == "online"
        assert tracker.get_health("recover")["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_rapid_flapping_handled(self):
        """Rapid online/offline/online transitions are tracked correctly."""
        from zpilot.monitor import NodeHealthTracker
        from zpilot.nodes import NodeRegistry

        node = self._make_node("flapper", alive=True)
        registry = NodeRegistry([node])
        tracker = NodeHealthTracker(registry, offline_threshold=1, degraded_threshold=1)

        # Online
        await tracker.check_node(node)
        assert tracker.get_health("flapper")["state"] == "online"

        # Offline
        node._transport.alive_flag = False
        await tracker.check_node(node)
        assert tracker.get_health("flapper")["state"] == "offline"

        # Back online
        node._transport.alive_flag = True
        await tracker.check_node(node)
        assert tracker.get_health("flapper")["state"] == "online"

        # Offline again
        node._transport.alive_flag = False
        await tracker.check_node(node)
        assert tracker.get_health("flapper")["state"] == "offline"

        # Verify failure count reset on each recovery
        node._transport.alive_flag = True
        await tracker.check_node(node)
        assert tracker.get_health("flapper")["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_degraded_state_for_high_latency_nodes(self):
        """Degraded state with consecutive failures below offline threshold."""
        from zpilot.monitor import NodeHealthTracker
        from zpilot.nodes import Node, NodeRegistry

        node = self._make_node("slow", alive=False)
        registry = NodeRegistry([node])
        tracker = NodeHealthTracker(
            registry, degraded_threshold=1, offline_threshold=5
        )

        # Single failure → degraded, not offline
        await tracker.check_node(node)
        h = tracker.get_health("slow")
        assert h["state"] == "degraded"
        assert h["consecutive_failures"] == 1

    @pytest.mark.asyncio
    async def test_last_seen_updated_on_success(self):
        """last_seen timestamp is updated when node is healthy."""
        from zpilot.monitor import NodeHealthTracker
        from zpilot.nodes import NodeRegistry

        node = self._make_node("timestamped", alive=True)
        registry = NodeRegistry([node])
        tracker = NodeHealthTracker(registry)

        await tracker.check_node(node)
        h = tracker.get_health("timestamped")
        assert h["last_seen"] is not None
        assert h["last_seen"] > 0

    @pytest.mark.asyncio
    async def test_error_field_set_on_failure(self):
        """Error field records the failure reason."""
        from zpilot.monitor import NodeHealthTracker
        from zpilot.nodes import NodeRegistry

        node = self._make_node("errornode", alive=False)
        registry = NodeRegistry([node])
        tracker = NodeHealthTracker(registry, offline_threshold=1)

        await tracker.check_node(node)
        h = tracker.get_health("errornode")
        assert h["error"] is not None
        assert "node down" in h["error"]

    @pytest.mark.asyncio
    async def test_error_cleared_on_recovery(self):
        """Error field is cleared when node recovers."""
        from zpilot.monitor import NodeHealthTracker
        from zpilot.nodes import NodeRegistry

        node = self._make_node("cleaner", alive=False)
        registry = NodeRegistry([node])
        tracker = NodeHealthTracker(registry, offline_threshold=1)

        await tracker.check_node(node)
        assert tracker.get_health("cleaner")["error"] is not None

        node._transport.alive_flag = True
        await tracker.check_node(node)
        assert tracker.get_health("cleaner")["error"] is None
