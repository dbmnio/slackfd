"""Client transport for the sfd daemon (Unix socket or loopback TCP)."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

from slack_fd.errors import DaemonUnavailable, SendFailed, SlackFdError
from slack_fd.ipc import ipc_mode, read_daemon_token, read_tcp_endpoint
from slack_fd.keys import StreamKey
from slack_fd.paths import daemon_sock_path, default_data_dir
from slack_fd.records import MessageRecord
from slack_fd.rpc import RpcError, recv_msg, send_msg


class DaemonTransport:
    """Talks to sfd-daemon over Unix domain socket or authenticated loopback TCP."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).resolve()
        self.mode = ipc_mode()
        self.sock_path = daemon_sock_path(self.data_dir)
        self._sock: socket.socket | None = None
        self._token: str | None = None
        self._lock = threading.Lock()
        self._next_id = 1

    def connect(self) -> None:
        if self._sock is not None:
            return
        if self.mode == "tcp":
            self._connect_tcp()
        else:
            self._connect_unix()
        self._call("ping")

    def _connect_unix(self) -> None:
        if not self.sock_path.exists():
            raise DaemonUnavailable(
                f"daemon socket not found at {self.sock_path}; run `sfd daemon start`"
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.sock_path))
        except OSError as e:
            sock.close()
            raise DaemonUnavailable(
                f"cannot connect to daemon at {self.sock_path}: {e}"
            ) from e
        self._sock = sock
        self._token = None

    def _connect_tcp(self) -> None:
        endpoint = read_tcp_endpoint(self.data_dir)
        if endpoint is None:
            raise DaemonUnavailable(
                f"daemon TCP endpoint not found under {self.data_dir}; "
                "start with SFD_IPC=tcp `sfd daemon start`"
            )
        host, port = endpoint
        token = read_daemon_token(self.data_dir)
        if not token:
            raise DaemonUnavailable(
                f"daemon token missing at {self.data_dir / 'daemon.token'}"
            )
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.connect((host, port))
        except OSError as e:
            sock.close()
            raise DaemonUnavailable(f"cannot connect to daemon at {host}:{port}: {e}") from e
        self._sock = sock
        self._token = token

    def open(self, key: StreamKey) -> None:
        self.connect()
        self._call("open", key=key.format())

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def read(
        self,
        key: StreamKey,
        *,
        consumer_id: str,
        timeout: float | None,
        ack: bool,
    ) -> MessageRecord | None:
        self.connect()
        result = self._call(
            "read",
            key=key.format(),
            consumer_id=consumer_id,
            timeout=timeout,
            ack=ack,
        )
        return _message(result.get("message"))

    def read_at(self, key: StreamKey, offset: int) -> MessageRecord | None:
        self.connect()
        result = self._call("read_at", key=key.format(), offset=offset)
        return _message(result.get("message"))

    def write(
        self,
        key: StreamKey,
        text: str,
        *,
        user_id: str | None = None,
    ) -> MessageRecord:
        self.connect()
        try:
            result = self._call(
                "write",
                key=key.format(),
                text=text,
                user_id=user_id,
            )
        except SlackFdError as e:
            raise SendFailed(str(e)) from e
        msg = _message(result.get("message"))
        if msg is None:
            raise SendFailed("daemon returned empty write result")
        return msg

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        self.connect()
        self._call("ack", key=key.format(), consumer_id=consumer_id, offset=offset)

    def inject_inbound(
        self,
        key: StreamKey,
        text: str,
        *,
        user: str | None = None,
        ts: str | None = None,
        **_extra: object,
    ) -> MessageRecord:
        self.connect()
        result = self._call(
            "inject",
            key=key.format(),
            text=text,
            user=user,
            ts=ts,
        )
        msg = _message(result.get("message"))
        if msg is None:
            raise SlackFdError("daemon returned empty inject result")
        return msg

    def status(self) -> dict[str, Any]:
        self.connect()
        return self._call("status")

    def fetch(self, key: StreamKey, *, limit: int = 200) -> dict[str, Any]:
        self.connect()
        return self._call("fetch", key=key.format(), limit=limit)

    def sync(self, key: StreamKey, *, limit: int = 200) -> dict[str, Any]:
        """Deprecated alias for :meth:`fetch`."""
        return self.fetch(key, limit=limit)

    def flush(
        self,
        key: StreamKey,
        *,
        retry_failed: bool = True,
        repair_first: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.connect()
        return self._call(
            "flush",
            key=key.format(),
            retry_failed=retry_failed,
            repair_first=repair_first,
            limit=limit,
        )

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        with self._lock:
            if self._sock is None:
                raise DaemonUnavailable("not connected")
            req_id = self._next_id
            self._next_id += 1
            payload: dict[str, Any] = {
                "id": req_id,
                "method": method,
                "params": params,
            }
            if self._token is not None:
                payload["token"] = self._token
            try:
                send_msg(self._sock, payload)
                resp = recv_msg(self._sock)
            except (OSError, ConnectionError) as e:
                self.close()
                raise DaemonUnavailable(f"daemon connection lost: {e}") from e
            if resp.get("id") != req_id:
                raise SlackFdError(f"RPC id mismatch: expected {req_id}, got {resp.get('id')}")
            if "error" in resp and resp["error"]:
                err = resp["error"]
                code = err.get("code", "rpc_error")
                message = err.get("message", "RPC error")
                if code in {"daemon_unavailable", "unauthorized"}:
                    raise DaemonUnavailable(message)
                raise RpcError(code, message)
            result = resp.get("result")
            if not isinstance(result, dict):
                return {}
            return result


def _message(data: Any) -> MessageRecord | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise SlackFdError("invalid message payload from daemon")
    return MessageRecord.from_dict(data)
