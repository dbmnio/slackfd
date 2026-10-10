"""In-process daemon service: logstore + waiters + optional Slack mux."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slack_fd.daemon.demux import demux_message_event
from slack_fd.daemon.mux import apply_send_result, send_message
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
        rec = MessageRecord(
            offset=-1,
            direction="out",
            stream=key.format(),
            text=text,
            user=user_id or key.user_id,
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            status="pending",
            client_msg_id=str(uuid.uuid4()),
        )
        written = self.store.append(key, rec, log="outbox")

        if self.slack and self.slack.client is not None:
            try:
                # Prefer explicit user_id for dm/app if provided on write.
                send_key = key
                if key.kind in {"dm", "app"} and user_id:
                    send_key = StreamKey(kind=key.kind, user_id=user_id)
                data = send_message(self.slack.client, send_key, text)
                status, ts, error = apply_send_result(written, data)
                updated = self.store.update_outbox_status(
                    key, written.offset, status, ts=ts, error=error  # type: ignore[arg-type]
                )
                if updated is not None:
                    return updated
            except Exception as e:
                log.exception("slack send failed for %s", key)
                updated = self.store.update_outbox_status(
                    key, written.offset, "failed", error=str(e)
                )
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
        return self._append_inbox(key, rec)

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
            written.append(self._append_inbox(key, rec))
            log.info(
                "inbox append stream=%s offset=%s user=%s text=%r",
                key.format(),
                written[-1].offset,
                demuxed.user,
                demuxed.text[:80],
            )
        return written

    def _append_inbox(self, key: StreamKey, rec: MessageRecord) -> MessageRecord:
        written = self.store.append(key, rec, log="inbox")
        with self._cond:
            self._cond.notify_all()
        return written

    def sync_stream(self, key: StreamKey, *, limit: int = 200) -> dict[str, Any]:
        """Catch-up from Slack history; wakes inbox waiters if anything was added."""
        from slack_fd.sync import sync_stream

        client = self.slack.client if self.slack else None
        result = sync_stream(
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

    def flush_stream(
        self,
        key: StreamKey,
        *,
        retry_failed: bool = True,
        repair_first: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Send pending/failed outbox rows to Slack (after optional history repair)."""
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
