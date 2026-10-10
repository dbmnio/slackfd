"""Length-prefixed JSON RPC over a stream (Unix domain socket)."""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

from slack_fd.errors import SlackFdError

_HEADER = struct.Struct("!I")  # uint32 big-endian payload length


class RpcError(SlackFdError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def send_msg(sock: BinaryIO | Any, obj: dict[str, Any]) -> None:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def recv_msg(sock: BinaryIO | Any) -> dict[str, Any]:
    header = _recvexact(sock, _HEADER.size)
    if not header:
        raise ConnectionError("connection closed")
    (length,) = _HEADER.unpack(header)
    if length > 16 * 1024 * 1024:
        raise RpcError("payload_too_large", f"RPC payload {length} bytes exceeds limit")
    raw = _recvexact(sock, length)
    if len(raw) != length:
        raise ConnectionError("connection closed mid-message")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RpcError("invalid_message", "RPC message must be a JSON object")
    return data


def _recvexact(sock: Any, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)
