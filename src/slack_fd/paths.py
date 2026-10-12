"""Data directory and socket paths."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# macOS AF_UNIX sun_path is short (~104 bytes). Keep headroom for safety.
_MAX_SOCK_PATH_LEN = 90


def default_data_dir() -> Path:
    override = os.environ.get("SFD_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser().resolve() / "sfd"
    return Path.home() / ".local" / "share" / "sfd"


def streams_dir(data_dir: Path | None = None) -> Path:
    root = data_dir or default_data_dir()
    return root / "streams"


def daemon_pid_path(data_dir: Path | None = None) -> Path:
    root = data_dir or default_data_dir()
    return root / "daemon.pid"


def daemon_lock_path(data_dir: Path | None = None) -> Path:
    """Exclusive flock path used to serialize ``daemon start``."""
    root = data_dir or default_data_dir()
    return root / "daemon.lock"


def daemon_sockpath_file(data_dir: Path | None = None) -> Path:
    """File that records the actual Unix socket path in use."""
    root = data_dir or default_data_dir()
    return root / "daemon.sockpath"


def daemon_sock_path(data_dir: Path | None = None) -> Path:
    """Return the Unix domain socket path for the daemon.

    Prefers ``<data_dir>/daemon.sock`` when short enough; otherwise a hashed
    path under ``$TMPDIR`` (AF_UNIX path length limits on macOS).
    """
    override = os.environ.get("SFD_SOCKET")
    if override:
        return Path(override).expanduser().resolve()

    root = (data_dir or default_data_dir()).resolve()
    recorded = daemon_sockpath_file(root)
    if recorded.exists():
        text = recorded.read_text(encoding="utf-8").strip()
        if text:
            return Path(text)

    return choose_sock_path(root)


def choose_sock_path(data_dir: Path) -> Path:
    """Pick a socket path for a new daemon instance (does not read sockpath file)."""
    override = os.environ.get("SFD_SOCKET")
    if override:
        return Path(override).expanduser().resolve()

    root = data_dir.resolve()
    candidate = root / "daemon.sock"
    if len(str(candidate)) <= _MAX_SOCK_PATH_LEN:
        return candidate

    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    tmp = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
    return tmp / f"sfd-{digest}.sock"


def write_sockpath_file(data_dir: Path, sock_path: Path) -> None:
    daemon_sockpath_file(data_dir).write_text(str(sock_path) + "\n", encoding="utf-8")
