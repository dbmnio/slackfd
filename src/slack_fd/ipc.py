"""IPC mode: Unix domain socket (default) or loopback TCP + auth token."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Literal

from slack_fd.paths import default_data_dir

IpcMode = Literal["unix", "tcp"]

# Fixed default local port; override with SFD_TCP_PORT.
_DEFAULT_TCP_PORT = 4173
_LOOPBACK = frozenset({"127.0.0.1", "::1"})


def ipc_mode() -> IpcMode:
    raw = (os.environ.get("SFD_IPC") or "unix").strip().lower()
    if raw not in {"unix", "tcp"}:
        raise ValueError("SFD_IPC must be 'unix' or 'tcp'")
    return raw  # type: ignore[return-value]


def tcp_bind_host() -> str:
    host = (os.environ.get("SFD_TCP_HOST") or "127.0.0.1").strip()
    if host == "localhost":
        host = "127.0.0.1"
    if host not in _LOOPBACK:
        raise ValueError(
            f"SFD_TCP_HOST must be loopback ({', '.join(sorted(_LOOPBACK))}), got {host!r}"
        )
    return host


def tcp_bind_port() -> int:
    return int(os.environ.get("SFD_TCP_PORT") or _DEFAULT_TCP_PORT)


def daemon_token_path(data_dir: Path | None = None) -> Path:
    root = data_dir or default_data_dir()
    return root / "daemon.token"


def daemon_tcp_path(data_dir: Path | None = None) -> Path:
    root = data_dir or default_data_dir()
    return root / "daemon.tcp"


def ensure_daemon_token(data_dir: Path) -> str:
    """Return IPC auth token; create a new one if missing (mode 0600)."""
    path = daemon_token_path(data_dir)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def read_daemon_token(data_dir: Path | None = None) -> str | None:
    path = daemon_token_path(data_dir)
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def write_tcp_endpoint(data_dir: Path, host: str, port: int) -> None:
    daemon_tcp_path(data_dir).write_text(f"{host}:{port}\n", encoding="utf-8")


def read_tcp_endpoint(data_dir: Path | None = None) -> tuple[str, int] | None:
    path = daemon_tcp_path(data_dir)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text or ":" not in text:
        return None
    host, _, port_s = text.rpartition(":")
    if host == "localhost":
        host = "127.0.0.1"
    if host not in _LOOPBACK:
        return None
    try:
        return host, int(port_s)
    except ValueError:
        return None


def tokens_match(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)
