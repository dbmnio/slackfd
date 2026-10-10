"""In-process transport backed by LogStore (no Slack, no daemon)."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord


class FakeTransport:
    """Local logstore-only transport for tests and Phase 0 CLI."""

    def __init__(self, data_dir: Path | None = None) -> None:
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
        rec = MessageRecord(
            offset=-1,
            direction="out",
            stream=key.format(),
            text=text,
            user=user_id or key.user_id,
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            status="sent",
            client_msg_id=str(uuid.uuid4()),
            ts=f"fake-{time.time():.6f}",
        )
        written = self._store.append(key, rec, log="outbox")
        # Phase 0: also mirror outbound into a side channel? Spec says outbox
        # is separate; read() is inbox-only. Callers use `log` CLI for outbox.
        return written

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        self._store.ack(key, consumer_id, offset)

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
