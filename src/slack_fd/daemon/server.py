"""Unix domain socket RPC server for the sfd daemon."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from pathlib import Path
from typing import Any

from slack_fd.config import load_config
from slack_fd.daemon.service import DaemonService
from slack_fd.daemon.slack_conn import SlackBridge
from slack_fd.ipc import (
    daemon_tcp_path,
    ensure_daemon_token,
    ipc_mode,
    tcp_bind_host,
    tcp_bind_port,
    tokens_match,
    write_tcp_endpoint,
)
from slack_fd.keys import StreamKey
from slack_fd.paths import (
    choose_sock_path,
    daemon_pid_path,
    daemon_sockpath_file,
    default_data_dir,
    write_sockpath_file,
)
from slack_fd.records import MessageRecord
from slack_fd.rpc import RpcError, recv_msg, send_msg

log = logging.getLogger("slack_fd.daemon")


class DaemonServer:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self.mode = ipc_mode()
        self.sock_path = choose_sock_path(self.data_dir)
        self.pid_path = daemon_pid_path(self.data_dir)
        self.service = DaemonService(self.data_dir)
        self._auth_token: str | None = None
        self._listen_label = ""
        self._slack: SlackBridge | None = None
        self._sock: socket.socket | None = None
        self._stopping = threading.Event()

    def serve_forever(self) -> None:
        self._install_signals()
        self._bind()
        self._write_pid()
        self._start_slack()
        assert self._sock is not None
        log.info("daemon listening on %s (pid %s)", self._listen_label, os.getpid())
        try:
            while not self._stopping.is_set():
                try:
                    self._sock.settimeout(0.5)
                    conn, _ = self._sock.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True,
                ).start()
        finally:
            self._cleanup()

    def stop(self) -> None:
        self._stopping.set()
        if self._slack is not None:
            self._slack.stop()
            self._slack = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _start_slack(self) -> None:
        cfg = load_config(self.data_dir)
        if not cfg.slack_ready:
            log.warning(
                "Slack tokens not configured; daemon running without Socket Mode. "
                "Set SLACK_BOT_TOKEN + SLACK_APP_TOKEN (see SLACK_SETUP.md)."
            )
            return
        try:
            bridge = SlackBridge(cfg, self.service)
            bridge.start()
            self._slack = bridge
            self.service.slack = bridge
        except Exception:
            log.exception(
                "failed to start Slack Socket Mode; continuing with local inject only"
            )

    def _install_signals(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            log.info("received signal %s; shutting down", signum)
            self.stop()

        # Signals can only be set from the main thread (subprocess daemon).
        try:
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
        except ValueError:
            log.debug("skipping signal handlers (not in main thread)")

    def _bind(self) -> None:
        if self.mode == "tcp":
            host = tcp_bind_host()
            port = tcp_bind_port()  # 0 = ephemeral; recorded in daemon.tcp
            family = socket.AF_INET6 if host == "::1" else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(64)
            bound_port = int(sock.getsockname()[1])
            self._sock = sock
            self._auth_token = ensure_daemon_token(self.data_dir)
            write_tcp_endpoint(self.data_dir, host, bound_port)
            self._listen_label = f"tcp://{host}:{bound_port} (token auth required)"
            return

        if self.sock_path.exists():
            self.sock_path.unlink()
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        sock.listen(64)
        self._sock = sock
        write_sockpath_file(self.data_dir, self.sock_path)
        self._listen_label = f"unix://{self.sock_path}"

    def _write_pid(self) -> None:
        self.pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    def _cleanup(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        paths = [self.pid_path]
        if self.mode == "tcp":
            paths.append(daemon_tcp_path(self.data_dir))
        else:
            paths.extend([self.sock_path, daemon_sockpath_file(self.data_dir)])
        for path in paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        log.info("daemon stopped")

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            while not self._stopping.is_set():
                try:
                    req = recv_msg(conn)
                except ConnectionError:
                    return
                except Exception as e:
                    log.warning("bad request: %s", e)
                    return
                resp = self._dispatch(req)
                try:
                    send_msg(conn, resp)
                except OSError:
                    return

    def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        try:
            if self._auth_token is not None:
                if not tokens_match(
                    req.get("token") if isinstance(req.get("token"), str) else None,
                    self._auth_token,
                ):
                    raise RpcError("unauthorized", "invalid or missing IPC token")
            if not isinstance(method, str):
                raise RpcError("invalid_request", "method must be a string")
            if not isinstance(params, dict):
                raise RpcError("invalid_request", "params must be an object")
            result = self._call(method, params)
            return {"id": req_id, "result": result}
        except RpcError as e:
            return {"id": req_id, "error": {"code": e.code, "message": e.message}}
        except Exception as e:
            log.exception("handler error for %s", method)
            return {
                "id": req_id,
                "error": {"code": "internal", "message": str(e)},
            }

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"pong": True}
        if method == "status":
            return self.service.status()

        key_s = params.get("key")
        key = StreamKey.parse(key_s) if key_s else None

        if method == "open":
            if key is None:
                raise RpcError("invalid_params", "key required")
            self.service.open(key)
            return {"ok": True}

        if method == "read":
            if key is None:
                raise RpcError("invalid_params", "key required")
            msg = self.service.read(
                key,
                consumer_id=str(params.get("consumer_id", "default")),
                timeout=params.get("timeout"),
                ack=bool(params.get("ack", True)),
            )
            return {"message": _msg_or_none(msg)}

        if method == "read_at":
            if key is None:
                raise RpcError("invalid_params", "key required")
            msg = self.service.read_at(key, int(params["offset"]))
            return {"message": _msg_or_none(msg)}

        if method == "write":
            if key is None:
                raise RpcError("invalid_params", "key required")
            msg = self.service.write(
                key,
                str(params.get("text", "")),
                user_id=params.get("user_id"),
            )
            return {"message": msg.to_dict()}

        if method == "ack":
            if key is None:
                raise RpcError("invalid_params", "key required")
            self.service.ack(
                key,
                str(params.get("consumer_id", "default")),
                int(params["offset"]),
            )
            return {"ok": True}

        if method == "inject":
            if key is None:
                raise RpcError("invalid_params", "key required")
            msg = self.service.inject(
                key,
                str(params.get("text", "")),
                user=params.get("user"),
                ts=params.get("ts"),
            )
            return {"message": msg.to_dict()}

        if method in {"fetch", "sync"}:  # sync = deprecated alias
            if key is None:
                raise RpcError("invalid_params", "key required")
            limit = int(params.get("limit", 200))
            return self.service.fetch_stream(key, limit=limit)

        if method == "flush":
            if key is None:
                raise RpcError("invalid_params", "key required")
            return self.service.flush_stream(
                key,
                retry_failed=bool(params.get("retry_failed", True)),
                repair_first=bool(params.get("repair_first", True)),
                limit=int(params.get("limit", 200)),
            )

        raise RpcError("unknown_method", f"unknown method {method!r}")


def _msg_or_none(msg: MessageRecord | None) -> dict[str, Any] | None:
    return None if msg is None else msg.to_dict()


def run(data_dir: Path | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    DaemonServer(data_dir).serve_forever()
