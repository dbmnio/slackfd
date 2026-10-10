"""In-process Socket Mode transport when the daemon is not running."""

from __future__ import annotations

import atexit
import logging
import threading
import warnings
from pathlib import Path

from slack_fd.config import load_config
from slack_fd.daemon.service import DaemonService
from slack_fd.daemon.slack_conn import SlackBridge
from slack_fd.keys import StreamKey
from slack_fd.paths import default_data_dir
from slack_fd.records import MessageRecord

log = logging.getLogger("slack_fd.embed")

_lock = threading.Lock()
_runtimes: dict[str, "_EmbedRuntime"] = {}


class _EmbedRuntime:
    """One shared in-process service (+ optional Slack bridge) per data_dir."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.service = DaemonService(data_dir)
        self.bridge: SlackBridge | None = None
        self._started = False

    def ensure_started(self) -> None:
        if self._started:
            return
        cfg = load_config(self.data_dir)
        if cfg.slack_ready:
            try:
                bridge = SlackBridge(cfg, self.service)
                bridge.start()
                self.bridge = bridge
                self.service.slack = bridge
                log.info("embed Socket Mode started for %s", self.data_dir)
            except Exception:
                log.exception(
                    "embed failed to start Socket Mode; continuing with local logs only"
                )
        else:
            log.warning(
                "embed mode without Slack tokens; local logs only "
                "(set SLACK_BOT_TOKEN + SLACK_APP_TOKEN)"
            )
        self._started = True

    def stop(self) -> None:
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
            self.service.slack = None
        self._started = False


def _runtime(data_dir: Path | None) -> _EmbedRuntime:
    root = (data_dir or default_data_dir()).resolve()
    key = str(root)
    with _lock:
        rt = _runtimes.get(key)
        if rt is None:
            rt = _EmbedRuntime(root)
            _runtimes[key] = rt
        return rt


def _shutdown_all() -> None:
    with _lock:
        for rt in _runtimes.values():
            try:
                rt.stop()
            except Exception:
                log.exception("embed shutdown error")
        _runtimes.clear()


atexit.register(_shutdown_all)


class EmbedTransport:
    """Local DaemonService + optional in-process Socket Mode (solo use)."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._rt = _runtime(data_dir)
        self._warned = False

    def open(self, key: StreamKey) -> None:
        if not self._warned:
            warnings.warn(
                "sfd daemon is not running; embedding Socket Mode in-process. "
                "Slack allows one active Socket Mode connection per app — "
                "use `sfd daemon start` for shared multi-process use.",
                UserWarning,
                stacklevel=2,
            )
            self._warned = True
        self._rt.ensure_started()
        self._rt.service.open(key)

    def close(self) -> None:
        # Keep the shared runtime for the process lifetime.
        return None

    def read(
        self,
        key: StreamKey,
        *,
        consumer_id: str,
        timeout: float | None,
        ack: bool,
    ) -> MessageRecord | None:
        self._rt.ensure_started()
        return self._rt.service.read(
            key, consumer_id=consumer_id, timeout=timeout, ack=ack
        )

    def read_at(self, key: StreamKey, offset: int) -> MessageRecord | None:
        self._rt.ensure_started()
        return self._rt.service.read_at(key, offset)

    def write(
        self,
        key: StreamKey,
        text: str,
        *,
        user_id: str | None = None,
    ) -> MessageRecord:
        self._rt.ensure_started()
        return self._rt.service.write(key, text, user_id=user_id)

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        self._rt.ensure_started()
        self._rt.service.ack(key, consumer_id, offset)

    def inject_inbound(
        self,
        key: StreamKey,
        text: str,
        *,
        user: str | None = None,
        ts: str | None = None,
        **_extra: object,
    ) -> MessageRecord:
        self._rt.ensure_started()
        return self._rt.service.inject(key, text, user=user, ts=ts)
