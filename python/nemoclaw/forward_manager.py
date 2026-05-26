# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
ForwardManager: SDK-based local port forward to a sandbox.

Replaces `openshell forward start --background` (which requires an SSH client
binary). Uses the same HTTP CONNECT tunnel the CLI uses, but drives it with
paramiko instead of spawning ssh(1).

Protocol (matches OpenShell's sandbox_ssh_proxy Rust implementation):
  1. TCP connect to the gateway host:port.
  2. Send `CONNECT <connect_path> HTTP/1.1` with X-Sandbox-Id / X-Sandbox-Token.
  3. Gateway responds 200 OK; the TCP stream is now a raw pipe to the sandbox
     SSH server.
  4. Start a paramiko Transport on the stream, authenticate as "sandbox".
  5. Accept connections on a local TCP server; for each one, open a
     direct-tcpip channel to localhost:<remote_port> inside the sandbox.
  6. Bridge bytes between the local socket and the paramiko channel.
"""

from __future__ import annotations

import logging
import select
import socket
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from openshell.sandbox import SandboxClient

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_CHANNEL_TIMEOUT = 10.0
_BRIDGE_CHUNK = 4096


class ForwardError(RuntimeError):
    pass


class ForwardManager:
    """Manages background SDK-based port forwards to sandbox OpenClaw ports."""

    def __init__(self, gateway_endpoint: str) -> None:
        self._endpoint = gateway_endpoint
        self._lock = threading.Lock()
        # sandbox_name → (server_socket, stop_event, thread)
        self._forwards: dict[str, tuple[socket.socket, threading.Event, threading.Thread]] = {}

    def start(
        self,
        local_port: int,
        sandbox_name: str,
        sandbox_client: "SandboxClient",
    ) -> tuple[bool, str]:
        """Start a background forward. Returns (ok, message)."""
        try:
            ref = sandbox_client.get(sandbox_name)
            sess = sandbox_client.create_ssh_session(ref.id)
        except Exception as exc:
            return False, f"create_ssh_session failed: {exc}"

        parsed = urlparse(self._endpoint)
        gw_host = parsed.hostname or "127.0.0.1"
        gw_port = parsed.port or (443 if (parsed.scheme or "http") == "https" else 80)

        stop = threading.Event()
        try:
            srv = _bind_server(local_port)
        except OSError as exc:
            return False, f"could not bind port {local_port}: {exc}"

        t = threading.Thread(
            target=_forward_loop,
            args=(srv, gw_host, gw_port, sess.connect_path, ref.id, sess.token, local_port, stop),
            daemon=True,
            name=f"forward-{sandbox_name}-{local_port}",
        )
        t.start()

        with self._lock:
            self._forwards[sandbox_name] = (srv, stop, t)

        return True, f"Forward active on port {local_port}"

    def stop(self, sandbox_name: str) -> None:
        with self._lock:
            entry = self._forwards.pop(sandbox_name, None)
        if entry is None:
            return
        srv, stop, t = entry
        stop.set()
        try:
            srv.close()
        except OSError:
            pass
        t.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bind_server(port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(10)
    srv.settimeout(1.0)
    return srv


def _http_connect(
    gw_host: str,
    gw_port: int,
    connect_path: str,
    sandbox_id: str,
    token: str,
) -> socket.socket:
    """Open a raw TCP socket and send HTTP CONNECT to create the SSH tunnel."""
    sock = socket.create_connection((gw_host, gw_port), timeout=_CONNECT_TIMEOUT)
    request = (
        f"CONNECT {connect_path} HTTP/1.1\r\n"
        f"Host: {gw_host}\r\n"
        f"X-Sandbox-Id: {sandbox_id}\r\n"
        f"X-Sandbox-Token: {token}\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode())

    # Read the response line by line until we hit the blank line.
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ForwardError("gateway closed connection during CONNECT handshake")
        resp += chunk

    status_line = resp.split(b"\r\n", 1)[0].decode(errors="replace")
    parts = status_line.split(" ", 2)
    code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    if code != 200:
        raise ForwardError(f"gateway CONNECT failed with status {code}: {status_line}")

    # Any bytes beyond the header boundary are the start of the SSH banner.
    # The socket now speaks raw SSH, so hand it back as-is.
    return sock


def _build_transport(sock: socket.socket) -> "paramiko.Transport":
    import paramiko

    transport = paramiko.Transport(sock)
    transport.start_client(timeout=_CONNECT_TIMEOUT)
    try:
        transport.auth_none("sandbox")
    except paramiko.AuthenticationException:
        pass
    if not transport.is_authenticated():
        try:
            transport.auth_password("sandbox", "")
        except paramiko.AuthenticationException:
            pass
    if not transport.is_authenticated():
        raise ForwardError("SSH authentication failed")
    return transport


def _forward_loop(
    srv: socket.socket,
    gw_host: str,
    gw_port: int,
    connect_path: str,
    sandbox_id: str,
    token: str,
    remote_port: int,
    stop: threading.Event,
) -> None:
    """Accept loop: for each local connection, open a direct-tcpip channel."""
    import paramiko

    transport: "paramiko.Transport | None" = None
    try:
        tunnel_sock = _http_connect(gw_host, gw_port, connect_path, sandbox_id, token)
        transport = _build_transport(tunnel_sock)
        logger.info("SSH transport established for forward on port %d", remote_port)
    except Exception as exc:
        logger.warning("Forward setup failed: %s", exc)
        return

    while not stop.is_set():
        try:
            client_sock, addr = srv.accept()
        except OSError:
            break

        try:
            chan = transport.open_channel(
                "direct-tcpip",
                ("localhost", remote_port),
                addr,
                timeout=_CHANNEL_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("direct-tcpip open failed: %s", exc)
            client_sock.close()
            continue

        t = threading.Thread(
            target=_bridge,
            args=(client_sock, chan),
            daemon=True,
        )
        t.start()

    if transport is not None:
        transport.close()


def _bridge(sock: socket.socket, chan: "paramiko.Channel") -> None:
    """Bidirectionally bridge a plain socket and a paramiko channel."""
    chan.settimeout(0.0)
    sock.setblocking(False)
    try:
        while True:
            r, _, _ = select.select([sock, chan], [], [], 1.0)
            if sock in r:
                data = sock.recv(_BRIDGE_CHUNK)
                if not data:
                    break
                chan.send(data)
            if chan in r:
                data = chan.recv(_BRIDGE_CHUNK)
                if not data:
                    break
                sock.sendall(data)
    except Exception:
        pass
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
