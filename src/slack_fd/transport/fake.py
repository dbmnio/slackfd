"""In-process transport backed by LogStore (no Slack, no daemon)."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord


class FakeTransport:
    """Local logstore-only transport for tests and Phase 0 CLI."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir
        self._store = LogStore(data_dir)
        self._cond = threading.Condition()
        self._open_keys: set[str] = set()

    @property
    def store(self) -> LogStore:
        return self._store

    def open(self, key: StreamKey) -> None:
        self._store.stream_dir(key)
        self._open_keys.add(key.format())

    def close(self) -> None:
        self._open_keys.clear()

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
                msg = self._store.next_after_cursor(key, consumer_id)
                if msg is not None:
                    if ack:
                        self._store.ack(key, consumer_id, msg.offset)
                    return msg
                if timeout is not None:
                    remaining = deadline - time.monotonic()  # type: ignore[operator]
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=remaining)
                else:
                    self._cond.wait()

    def read_at(self, key: StreamKey, offset: int) -> MessageRecord | None:
        return self._store.read_at(key, offset, log="inbox")

    def write(
        self,
        key: StreamKey,
        text: str,
        *,
        user_id: str | None = None,
    ) -> MessageRecord:
        """Append pending outbox row (remote cursor advances via ``flush``)."""
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
        return self._store.append(key, rec, log="outbox")

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        self._store.ack(key, consumer_id, offset)

    def fetch(self, key: StreamKey, *, limit: int = 200) -> dict[str, Any]:
        """No Slack uplink — local-only no-op."""
        return {
            "key": key.format(),
            "fetched": 0,
            "inbound_added": 0,
            "outbox_repaired": 0,
            "notes": ["fake transport: fetch is a no-op"],
        }

    def flush(
        self,
        key: StreamKey,
        *,
        retry_failed: bool = True,
        repair_first: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Mark pending/failed outbox rows as sent with synthetic timestamps."""
        del repair_first, limit  # no remote history in fake mode
        sent = 0
        skipped = 0
        for rec in list(self._store.iter_from(key, 0, log="outbox")):
            if rec.status == "sent" and rec.ts:
                skipped += 1
                continue
            if rec.status == "failed" and not retry_failed:
                skipped += 1
                continue
            if rec.status not in {"pending", "failed"} and rec.ts:
                skipped += 1
                continue
            self._store.update_outbox_status(
                key,
                rec.offset,
                "sent",
                ts=rec.ts or f"fake-{time.time():.6f}",
            )
            sent += 1
        return {
            "key": key.format(),
            "repaired": 0,
            "sent": sent,
            "failed": 0,
            "skipped": skipped,
            "notes": ["fake transport: marked outbox sent locally"],
        }

    def inject_inbound(
        self,
        key: StreamKey,
        text: str,
        *,
        user: str | None = None,
        ts: str | None = None,
        **_extra: object,
    ) -> MessageRecord:
        """Simulate a Slack inbound message for tests / local demos."""
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
        written = self._store.append(key, rec, log="inbox")
        with self._cond:
            self._cond.notify_all()
        return written
