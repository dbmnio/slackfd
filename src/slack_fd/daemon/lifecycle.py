"""Start / stop / status for the background daemon process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from slack_fd.errors import DaemonUnavailable
from slack_fd.ipc import daemon_tcp_path, ipc_mode, read_tcp_endpoint
from slack_fd.paths import (
    daemon_pid_path,
    daemon_sock_path,
    daemon_sockpath_file,
    default_data_dir,
)
from slack_fd.transport.daemon import DaemonTransport


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()).resolve()


def is_running(data_dir: Path | None = None) -> bool:
    root = resolve_data_dir(data_dir)
    pid = _read_pid(root)
    if pid is None or not _pid_alive(pid):
        return False
    if ipc_mode() == "tcp":
        return read_tcp_endpoint(root) is not None
    return daemon_sock_path(root).exists()


def status(data_dir: Path | None = None) -> dict:
    root = resolve_data_dir(data_dir)
    pid = _read_pid(root)
    alive = pid is not None and _pid_alive(pid)
    mode = ipc_mode()
    if mode == "tcp":
        endpoint = read_tcp_endpoint(root)
        endpoint_s = f"{endpoint[0]}:{endpoint[1]}" if endpoint else None
        endpoint_ok = endpoint is not None
    else:
        sock = daemon_sock_path(root)
        endpoint_s = str(sock)
        endpoint_ok = sock.exists()

    info: dict = {
        "running": False,
        "pid": pid,
        "ipc": mode,
        "socket": endpoint_s,
        "data_dir": str(root),
        "pid_alive": alive,
        "socket_exists": endpoint_ok,
    }
    if alive and endpoint_ok:
        try:
            t = DaemonTransport(root)
            t.connect()
            info["running"] = True
            info["daemon"] = t.status()
            t.close()
        except DaemonUnavailable as e:
            info["error"] = str(e)
    return info


def start(data_dir: Path | None = None, *, timeout: float = 5.0) -> dict:
    root = resolve_data_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)

    if is_running(root):
        return {"started": False, "already_running": True, **status(root)}

    _cleanup_stale(root)

    log_path = root / "daemon.log"
    env = os.environ.copy()
    env["SFD_DATA_DIR"] = str(root)

    with log_path.open("a", encoding="utf-8") as logf:
        subprocess.Popen(
            [sys.executable, "-m", "slack_fd.daemon"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(root):
            return {"started": True, "already_running": False, **status(root)}
        time.sleep(0.05)

    raise DaemonUnavailable(
        f"daemon failed to start within {timeout}s; see {log_path}"
    )


def stop(data_dir: Path | None = None, *, timeout: float = 5.0) -> dict:
    root = resolve_data_dir(data_dir)
    pid = _read_pid(root)
    if pid is None or not _pid_alive(pid):
        _cleanup_stale(root)
        return {"stopped": False, "was_running": False, "data_dir": str(root)}

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _cleanup_stale(root)
            return {"stopped": True, "was_running": True, "pid": pid, "data_dir": str(root)}
        time.sleep(0.05)

    os.kill(pid, signal.SIGKILL)
    _cleanup_stale(root)
    return {
        "stopped": True,
        "was_running": True,
        "pid": pid,
        "forced": True,
        "data_dir": str(root),
    }


def _read_pid(data_dir: Path) -> int | None:
    path = daemon_pid_path(data_dir)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cleanup_stale(data_dir: Path) -> None:
    paths: list[Path | None] = [
        daemon_pid_path(data_dir),
        daemon_tcp_path(data_dir),
        daemon_sockpath_file(data_dir),
    ]
    try:
        paths.append(daemon_sock_path(data_dir))
    except OSError:
        pass
    for path in paths:
        if path is None:
            continue
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
