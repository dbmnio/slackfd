"""In-process daemon service: logstore + waiters + optional Slack mux."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slack_fd.daemon.demux import demux_message_event
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord

if TYPE_CHECKING:
    from slack_fd.daemon.slack_conn import SlackBridge

log = logging.getLogger("slack_fd.service")


class DaemonService:
    """Shared state for the daemon process."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.store = LogStore(data_dir)
        self._cond = threading.Condition()
        self.started_at = time.time()
        self.slack: SlackBridge | None = None

    def open(self, key: StreamKey) -> None:
        self.store.stream_dir(key)

    def read(
        self,
        key: StreamKey,
        *,
        consumer_id: str,
        timeout: float | None,
        ack: bool,
    ) -> MessageRecord | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                msg = self.store.next_after_cursor(key, consumer_id)
                if msg is not None:
                    if ack:
                        self.store.ack(key, consumer_id, msg.offset)
                    return msg
                if timeout is not None:
                    remaining = deadline - time.monotonic()  # type: ignore[operator]
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=remaining)
                else:
                    self._cond.wait()

    def read_at(self, key: StreamKey, offset: int) -> MessageRecord | None:
        return self.store.read_at(key, offset, log="inbox")

    def write(
        self,
        key: StreamKey,
        text: str,
        *,
        user_id: str | None = None,
    ) -> MessageRecord:
        """Enqueue outbox; when Slack is up, drain this stream (no client flush)."""
        rec = MessageRecord(
            offset=-1,
            direction="out",
            stream=key.format(),
            text=text,
            user=user_id,
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            status="pending",
            client_msg_id=str(uuid.uuid4()),
        )
        written = self.store.append(key, rec, log="outbox")
        if self.slack and self.slack.connected:
            try:
                # repair_first=False: avoid history HTTP on every send (rate limits).
                self.flush_stream(
                    key, retry_failed=True, repair_first=False, limit=200
                )
            except Exception:
                log.exception("daemon flush after write failed for %s", key)
            updated = self.store.read_at(key, written.offset, log="outbox")
            if updated is not None:
                return updated
        return written

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        self.store.ack(key, consumer_id, offset)

    def inject(
        self,
        key: StreamKey,
        text: str,
        *,
        user: str | None = None,
        ts: str | None = None,
    ) -> MessageRecord:
        rec = MessageRecord(
            offset=-1,
            direction="in",
            stream=key.format(),
            text=text,
            user=user or "U_FAKE",
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            ts=ts or f"fake-{time.time():.6f}",
        )
        written = self._append_inbox(key, rec)
        assert written is not None
        return written

    def ingest_slack_event(
        self,
        event: dict[str, Any],
        *,
        bot_user_id: str | None,
    ) -> list[MessageRecord]:
        demuxed = demux_message_event(event, bot_user_id=bot_user_id)
        if demuxed is None:
            log.info(
                "ignored slack message subtype=%s user=%s channel=%s bot=%s text=%r",
                event.get("subtype"),
                event.get("user"),
                event.get("channel"),
                bot_user_id,
                (event.get("text") or "")[:80],
            )
            return []
        written: list[MessageRecord] = []
        for key in demuxed.keys:
            rec = MessageRecord(
                offset=-1,
                direction="in",
                stream=key.format(),
                text=demuxed.text,
                user=demuxed.user,
                channel=demuxed.channel,
                thread_ts=demuxed.thread_ts,
                ts=demuxed.ts,
                raw=demuxed.raw,
            )
            appended = self._append_inbox(key, rec)
            if appended is None:
                log.info(
                    "inbox skip duplicate ts=%s stream=%s text=%r",
                    demuxed.ts,
                    key.format(),
                    demuxed.text[:80],
                )
                continue
            written.append(appended)
            log.info(
                "inbox append stream=%s offset=%s user=%s text=%r",
                key.format(),
                appended.offset,
                demuxed.user,
                demuxed.text[:80],
            )
        return written

    def _append_inbox(self, key: StreamKey, rec: MessageRecord) -> MessageRecord | None:
        """Append inbox row, or return None if this Slack ``ts`` is already present.

        Guards against Socket Mode retries and demux↔fetch races that would
        otherwise append the same message twice.
        """
        if rec.ts and self.store.inbox_has_ts(key, rec.ts):
            return None
        written = self.store.append(key, rec, log="inbox")
        with self._cond:
            self._cond.notify_all()
        return written

    def fetch_stream(self, key: StreamKey, *, limit: int = 200) -> dict[str, Any]:
        """Slack → local catch-up; wakes inbox waiters if anything was added."""
        from slack_fd.fetch import fetch_stream

        client = self.slack.client if self.slack else None
        result = fetch_stream(
            key,
            data_dir=self.data_dir,
            client=client,
            store=self.store,
            limit=limit,
        )
        if result.inbound_added:
            with self._cond:
                self._cond.notify_all()
        return result.to_dict()

    # Back-compat
    sync_stream = fetch_stream

    def flush_stream(
        self,
        key: StreamKey,
        *,
        retry_failed: bool = True,
        repair_first: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Send pending/failed outbox rows to Slack.

        Default ``repair_first=False`` for the websocket backend (send only).
        History repair remains available for admin/legacy callers.
        """
        from slack_fd.flush import flush_stream

        client = self.slack.client if self.slack else None
        result = flush_stream(
            key,
            data_dir=self.data_dir,
            client=client,
            store=self.store,
            retry_failed=retry_failed,
            repair_first=repair_first,
            limit=limit,
        )
        return result.to_dict()

    def reconcile_after_connect(self, *, limit: int = 200) -> dict[str, Any]:
        """Heal local streams from Slack history, then drain pending outbox.

        Called when Socket Mode comes up. Idempotent (inbox dedupes by ``ts``).
        """
        keys = self.store.list_streams()
        summary: dict[str, Any] = {
            "streams": len(keys),
            "fetched": 0,
            "inbound_added": 0,
            "flushed_sent": 0,
            "flushed_failed": 0,
            "errors": [],
        }
        for key in keys:
            try:
                fr = self.fetch_stream(key, limit=limit)
                summary["fetched"] += int(fr.get("fetched") or 0)
                summary["inbound_added"] += int(fr.get("inbound_added") or 0)
            except Exception as e:
                log.exception("heal fetch failed for %s", key)
                summary["errors"].append(f"fetch {key}: {e}")
            try:
                xr = self.flush_stream(
                    key, retry_failed=True, repair_first=False, limit=limit
                )
                summary["flushed_sent"] += int(xr.get("sent") or 0)
                summary["flushed_failed"] += int(xr.get("failed") or 0)
            except Exception as e:
                log.exception("heal flush failed for %s", key)
                summary["errors"].append(f"flush {key}: {e}")
        log.info(
            "reconcile_after_connect streams=%s inbound_added=%s sent=%s errors=%s",
            summary["streams"],
            summary["inbound_added"],
            summary["flushed_sent"],
            len(summary["errors"]),
        )
        return summary

    def status(self) -> dict[str, Any]:
        slack_on = bool(self.slack and self.slack.connected)
        return {
            "ok": True,
            "phase": 2 if slack_on else 1,
            "slack": slack_on,
            "bot_user_id": self.slack.bot_user_id if self.slack else None,
            "data_dir": str(self.data_dir),
            "uptime_s": round(time.time() - self.started_at, 3),
        }
