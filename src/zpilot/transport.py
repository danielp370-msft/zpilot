"""Transport layer — abstracts how zpilot talks to nodes."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger("zpilot.transport")


class CircuitBreaker:
    """Simple circuit breaker for transport resilience.

    States: CLOSED (normal) -> OPEN (failing, reject requests) -> HALF_OPEN (try one)
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = self.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self._half_open_permitted = False

    def record_success(self):
        self.failure_count = 0
        self.state = self.CLOSED
        self._half_open_permitted = False

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.state == self.HALF_OPEN:
            self.state = self.OPEN
        elif self.failure_count >= self.failure_threshold:
            self.state = self.OPEN

    def allow_request(self) -> bool:
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN:
            if time.monotonic() - self.last_failure_time >= self.recovery_timeout:
                self.state = self.HALF_OPEN
                self._half_open_permitted = False  # probe granted here
                return True
            return False
        # HALF_OPEN: only the single probe (granted above) is allowed
        if self._half_open_permitted:
            self._half_open_permitted = False
            return True
        return False

    @property
    def is_open(self) -> bool:
        return self.state == self.OPEN


@dataclass
class ExecResult:
    """Result of executing a command on a node."""
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Combined stdout, falling back to stderr on failure."""
        return self.stdout if self.ok else f"{self.stdout}\n{self.stderr}".strip()


class Transport(ABC):
    """Abstract transport protocol for communicating with nodes."""

    @abstractmethod
    async def exec(self, command: str, timeout: float = 30.0) -> ExecResult:
        """Execute a command and return the result."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the node is reachable."""

    @abstractmethod
    async def upload(self, local_path: str, remote_path: str) -> None:
        """Upload a file to the node."""

    @abstractmethod
    async def download(self, remote_path: str, local_path: str) -> None:
        """Download a file from the node."""

    async def read_file(self, path: str) -> str:
        """Read a text file on the node."""
        result = await self.exec(f"cat {shlex.quote(path)}")
        if not result.ok:
            raise FileNotFoundError(f"{path}: {result.stderr}")
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        """Write a text file on the node."""
        escaped = content.replace("'", "'\\''")
        result = await self.exec(f"mkdir -p $(dirname {shlex.quote(path)}) && "
                                  f"printf '%s' '{escaped}' > {shlex.quote(path)}")
        if not result.ok:
            raise IOError(f"Failed to write {path}: {result.stderr}")

    async def list_dir(self, path: str = ".") -> list[str]:
        """List files in a directory."""
        result = await self.exec(f"ls -1 {shlex.quote(path)}")
        if not result.ok:
            return []
        return [f for f in result.stdout.splitlines() if f.strip()]


class LocalTransport(Transport):
    """Transport for the local machine (direct subprocess)."""

    async def exec(self, command: str, timeout: float = 30.0) -> ExecResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return ExecResult(
                returncode=proc.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ExecResult(returncode=-1, stdout="", stderr="Command timed out")
        except Exception as e:
            return ExecResult(returncode=-1, stdout="", stderr=str(e))

    async def is_alive(self) -> bool:
        return True

    async def upload(self, local_path: str, remote_path: str) -> None:
        result = await self.exec(f"cp {shlex.quote(local_path)} {shlex.quote(remote_path)}")
        if not result.ok:
            raise IOError(result.stderr)

    async def download(self, remote_path: str, local_path: str) -> None:
        await self.upload(remote_path, local_path)  # same for local


class SSHTransport(Transport):
    """Transport via SSH (legacy).

    .. deprecated::
        SSH transport is maintained for backward compatibility but is no
        longer the recommended path for new deployments.  It requires
        direct SSH network access which is firewall/VPN dependent and
        difficult to use across heterogeneous networks.

        **Recommended alternative:** Use ``MCPTransport`` with an HTTP
        endpoint (optionally exposed via Azure devtunnel) for simpler
        setup, built-in retry/circuit-breaker resilience, and zero
        firewall configuration.

    Uses ~/.ssh/config for all connection details.  Supports
    ControlMaster for connection reuse — configure in your ssh_config
    with ControlPath/ControlPersist for best performance.
    """

    def __init__(
        self,
        host: str,
        user: str | None = None,
        port: int | None = None,
        identity_file: str | None = None,
        connect_timeout: int = 10,
        wsl_distro: str | None = None,
        wsl_user: str | None = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.connect_timeout = connect_timeout
        self.wsl_distro = wsl_distro
        self.wsl_user = wsl_user

    def _ssh_args(self) -> list[str]:
        """Build SSH command args."""
        args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.user:
            args += ["-l", self.user]
        if self.port:
            args += ["-p", str(self.port)]
        if self.identity_file:
            args += ["-i", self.identity_file]
        args.append(self.host)
        return args

    def _wrap_command(self, command: str) -> str:
        """Wrap command for WSL if needed."""
        if self.wsl_distro:
            user_flag = f"-u {self.wsl_user} " if self.wsl_user else ""
            escaped = command.replace("'", "'\\''")
            return f"wsl -d {self.wsl_distro} {user_flag}-- bash -lc '{escaped}'"
        return command

    # Transient SSH errors worth retrying
    _SSH_TRANSIENT_ERRORS = (
        "Connection refused",
        "Connection reset",
        "Connection timed out",
        "No route to host",
    )

    async def exec(self, command: str, timeout: float = 30.0, force_pty: bool = False) -> ExecResult:
        max_ssh_retries = 2
        ssh_retry_delay = 1.0

        for attempt in range(max_ssh_retries + 1):
            result = await self._exec_once(command, timeout, force_pty)
            # Retry on SSH connection failure (exit code 255) or transient errors
            if self._is_transient_ssh_failure(result) and attempt < max_ssh_retries:
                log.debug(
                    "SSH transient failure on %s (attempt %d/%d): %s",
                    self.host, attempt + 1, max_ssh_retries + 1, result.stderr.strip(),
                )
                await asyncio.sleep(ssh_retry_delay)
                continue
            return result
        return result  # unreachable but satisfies type checkers

    def _is_transient_ssh_failure(self, result: ExecResult) -> bool:
        """Check if an SSH result indicates a transient connection failure."""
        if result.returncode == 255:
            return True
        if result.returncode != 0:
            stderr = result.stderr
            for pattern in self._SSH_TRANSIENT_ERRORS:
                if pattern in stderr:
                    return True
        return False

    async def _exec_once(self, command: str, timeout: float = 30.0, force_pty: bool = False) -> ExecResult:
        wrapped = self._wrap_command(command)
        ssh_cmd = self._ssh_args() + [wrapped]
        if force_pty:
            # Insert -t after 'ssh' to force PTY allocation (needed for zellij dump-screen)
            ssh_cmd.insert(1, "-t")
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return ExecResult(
                returncode=proc.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ExecResult(returncode=-1, stdout="", stderr="SSH command timed out")
        except Exception as e:
            return ExecResult(returncode=-1, stdout="", stderr=str(e))

    async def is_alive(self) -> bool:
        result = await self.exec("echo zpilot-ping", timeout=10.0)
        return result.ok and "zpilot-ping" in result.stdout

    async def upload(self, local_path: str, remote_path: str) -> None:
        args = ["scp", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}"]
        if self.port:
            args += ["-P", str(self.port)]
        if self.identity_file:
            args += ["-i", self.identity_file]
        target = f"{self.user}@{self.host}" if self.user else self.host
        args += [local_path, f"{target}:{remote_path}"]

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise IOError(f"scp upload failed: {stderr.decode()}")

    async def download(self, remote_path: str, local_path: str) -> None:
        args = ["scp", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}"]
        if self.port:
            args += ["-P", str(self.port)]
        if self.identity_file:
            args += ["-i", self.identity_file]
        target = f"{self.user}@{self.host}" if self.user else self.host
        args += [f"{target}:{remote_path}", local_path]

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise IOError(f"scp download failed: {stderr.decode()}")


class MCPTransport(Transport):
    """Transport via HTTP — talks to a remote zpilot instance's REST API.

    Instead of SSH, this transport calls the remote zpilot's HTTP endpoints
    (/api/exec, /api/upload, /api/download) exposed by mcp_http.py.
    The MCP endpoint (/mcp) is used separately for tool-level communication.

    Includes automatic retry with exponential backoff for resilience against
    transient network failures. Configure via max_retries and retry_delay.

    A circuit breaker prevents wasting time on known-dead nodes: after
    failure_threshold consecutive failures, requests are short-circuited
    until recovery_timeout elapses.

    TLS verification can be controlled via verify_ssl and ca_cert parameters.
    For self-signed certs, set verify_ssl=False or pin the CA with ca_cert.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        verify_ssl: bool = False,
        ca_cert: str | None = None,
        cert_fingerprint: str | None = None,
        circuit_failure_threshold: int = 5,
        circuit_recovery_timeout: float = 30.0,
    ):
        # url should be the base URL like "https://host:8222"
        # Strip trailing /mcp if present
        self.base_url = url.rstrip("/").removesuffix("/mcp")
        self.token = token
        self.default_timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.verify_ssl = verify_ssl
        self.ca_cert = ca_cert
        self.cert_fingerprint = cert_fingerprint
        self._circuit = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            recovery_timeout=circuit_recovery_timeout,
        )

    @property
    def _verify(self) -> str | bool:
        """Return the verify parameter for httpx: CA path or bool."""
        if self.ca_cert:
            return self.ca_cert
        return self.verify_ssl

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _retry_request(self, coro_factory, operation: str = "request"):
        """Execute an async HTTP request with retry and exponential backoff.

        coro_factory: callable that takes an httpx.AsyncClient and returns a coroutine.
        Raises ConnectionError on exhausted retries.
        """
        import httpx

        if not self._circuit.allow_request():
            raise ConnectionError(
                f"{operation} rejected by circuit breaker (node unreachable)"
            )

        last_error = ""
        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(verify=self._verify) as client:
                    result = await coro_factory(client)
                    self._circuit.record_success()
                    return result
            except httpx.TimeoutException:
                last_error = f"{operation} timed out"
            except httpx.ConnectError as e:
                last_error = f"{operation} connection failed: {e}"
            except httpx.RemoteProtocolError as e:
                last_error = f"{operation} protocol error: {e}"
            except Exception as e:
                last_error = f"{operation} error: {e}"
            self._circuit.record_failure()
            if attempt < self.max_retries - 1:
                delay = self.retry_delay * (2 ** attempt)
                await asyncio.sleep(delay)
        raise ConnectionError(f"{last_error} (after {self.max_retries} attempts)")

    async def exec(self, command: str, timeout: float = 30.0, **kwargs) -> ExecResult:
        import httpx

        async def _do(client: httpx.AsyncClient):
            resp = await client.post(
                f"{self.base_url}/api/exec",
                json={"command": command, "timeout": timeout},
                headers=self._headers(),
                timeout=timeout + 10,
            )
            if resp.status_code == 401:
                return ExecResult(-1, "", "Authentication failed (401)")
            resp.raise_for_status()
            data = resp.json()
            return ExecResult(
                returncode=data.get("returncode", -1),
                stdout=data.get("stdout", ""),
                stderr=data.get("stderr", ""),
            )

        try:
            return await self._retry_request(_do, "exec")
        except ConnectionError as e:
            return ExecResult(-1, "", str(e))

    async def is_alive(self) -> bool:
        import httpx

        async def _do(client: httpx.AsyncClient):
            resp = await client.get(
                f"{self.base_url}/health",
                timeout=10.0,
            )
            return resp.status_code == 200

        try:
            return await self._retry_request(_do, "health")
        except ConnectionError:
            return False

    async def upload(self, local_path: str, remote_path: str) -> None:
        import base64
        import httpx

        with open(local_path, "rb") as f:
            content = base64.b64encode(f.read()).decode()

        async def _do(client: httpx.AsyncClient):
            resp = await client.post(
                f"{self.base_url}/api/upload",
                json={"path": remote_path, "content": content},
                headers=self._headers(),
                timeout=60.0,
            )
            if resp.status_code != 200:
                raise IOError(f"Upload failed: {resp.text}")

        try:
            await self._retry_request(_do, "upload")
        except ConnectionError as e:
            raise IOError(str(e)) from e

    async def download(self, remote_path: str, local_path: str) -> None:
        import base64
        import httpx

        async def _do(client: httpx.AsyncClient):
            resp = await client.get(
                f"{self.base_url}/api/download",
                params={"path": remote_path},
                headers=self._headers(),
                timeout=60.0,
            )
            if resp.status_code != 200:
                raise IOError(f"Download failed: {resp.text}")
            data = resp.json()
            content = base64.b64decode(data["content"])
            with open(local_path, "wb") as f:
                f.write(content)

        try:
            await self._retry_request(_do, "download")
        except ConnectionError as e:
            raise IOError(str(e)) from e


def create_transport(
    transport_type: str,
    host: str | None = None,
    **opts,
) -> Transport:
    """Factory: create a transport from config."""
    if transport_type == "local":
        return LocalTransport()
    elif transport_type == "ssh":
        if not host:
            raise ValueError("SSH transport requires 'host'")
        log.info(
            "SSH transport selected for host '%s'. Consider migrating to "
            "MCP transport (transport = \"mcp\") with serve-http + devtunnel "
            "for easier connectivity and built-in resilience.",
            host,
        )
        return SSHTransport(
            host=host,
            user=opts.get("user"),
            port=opts.get("port"),
            identity_file=opts.get("identity_file"),
            wsl_distro=opts.get("wsl_distro"),
            wsl_user=opts.get("wsl_user"),
        )
    elif transport_type == "mcp":
        url = host or opts.get("url")
        if not url:
            raise ValueError("MCP transport requires 'url' (or 'host')")
        return MCPTransport(
            url=url,
            token=opts.get("token"),
            timeout=opts.get("timeout", 30.0),
            max_retries=opts.get("max_retries", 3),
            retry_delay=opts.get("retry_delay", 2.0),
            verify_ssl=opts.get("verify_ssl", False),
            ca_cert=opts.get("ca_cert"),
            cert_fingerprint=opts.get("cert_fingerprint"),
        )
    else:
        raise ValueError(f"Unknown transport: {transport_type}")
