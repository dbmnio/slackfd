"""Unix-domain-socket client transport (Phase 1)."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

from slack_fd.errors import DaemonUnavailable, SendFailed, SlackFdError
from slack_fd.keys import StreamKey
from slack_fd.paths import daemon_sock_path, default_data_dir
from slack_fd.records import MessageRecord
from slack_fd.rpc import RpcError, recv_msg, send_msg


class DaemonTransport:
    """Talks to sfd-daemon over a Unix domain socket."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).resolve()
        self.sock_path = daemon_sock_path(self.data_dir)
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._next_id = 1

    def connect(self) -> None:
        if self._sock is not None:
            return
        if not self.sock_path.exists():
            raise DaemonUnavailable(
                f"daemon socket not found at {self.sock_path}; run `sfd daemon start`"
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.sock_path))
        except OSError as e:
            sock.close()
            raise DaemonUnavailable(f"cannot connect to daemon at {self.sock_path}: {e}") from e
        self._sock = sock
        self._call("ping")

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

    def sync(self, key: StreamKey, *, limit: int = 200) -> dict[str, Any]:
        self.connect()
        return self._call("sync", key=key.format(), limit=limit)

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        with self._lock:
            if self._sock is None:
                raise DaemonUnavailable("not connected")
            req_id = self._next_id
            self._next_id += 1
            try:
                send_msg(self._sock, {"id": req_id, "method": method, "params": params})
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
                if code in {"daemon_unavailable"}:
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
