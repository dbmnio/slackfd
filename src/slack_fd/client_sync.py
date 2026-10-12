"""Synchronous SlackStream client."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from slack_fd.errors import DaemonUnavailable, Timeout
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogName, LogStore
from slack_fd.records import MessageRecord
from slack_fd.transport.base import Transport
from slack_fd.transport.daemon import DaemonTransport
from slack_fd.transport.embed import EmbedTransport
from slack_fd.transport.fake import FakeTransport

log = logging.getLogger("slack_fd.client")

TransportName = Literal["fake", "daemon", "embed", "auto"]


class SlackStream:
    """File-descriptor-like sync handle for one conversation stream.

    Simple API (defaults):
      - ``read`` calls ``fetch`` when the local inbox cursor is at the tip
      - ``write`` appends to the outbox then ``flush`` until the gap is closed

    Advanced: pass ``autofetch=False`` / ``autoflush=False`` and call
    ``fetch`` / ``flush`` yourself.
    """

    def __init__(
        self,
        key: StreamKey | str,
        *,
        consumer_id: str = "default",
        transport: TransportName | Transport = "auto",
        data_dir: Path | None = None,
        raise_on_timeout: bool = False,
        autofetch: bool = True,
        autoflush: bool = True,
    ) -> None:
        self.key = StreamKey.parse(key) if isinstance(key, str) else key
        self.consumer_id = consumer_id
        self.raise_on_timeout = raise_on_timeout
        self.autofetch = autofetch
        self.autoflush = autoflush
        self._data_dir = data_dir
        self._owns_transport = isinstance(transport, str)
        self._transport = self._resolve_transport(transport, data_dir)
        self._closed = False

    @staticmethod
    def _resolve_transport(
        transport: TransportName | Transport,
        data_dir: Path | None,
    ) -> Transport:
        if not isinstance(transport, str):
            return transport

        # Explicit env override wins for named modes.
        env = os.environ.get("SFD_TRANSPORT")
        if transport == "auto" and env in {"fake", "daemon", "embed"}:
            transport = env  # type: ignore[assignment]

        if transport == "fake":
            return FakeTransport(data_dir)

        if transport == "embed":
            return EmbedTransport(data_dir)

        if transport == "daemon":
            t = DaemonTransport(data_dir)
            t.connect()
            return t

        # auto: daemon if up, else embed + warning
        try:
            t = DaemonTransport(data_dir)
            t.connect()
            return t
        except DaemonUnavailable:
            return EmbedTransport(data_dir)

    def _store(self) -> LogStore:
        t = self._transport
        store = getattr(t, "store", None)
        if isinstance(store, LogStore):
            return store
        data_dir = getattr(t, "data_dir", None) or self._data_dir
        return LogStore(data_dir)

    def __enter__(self) -> SlackStream:
        self._transport.open(self.key)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            if self._owns_transport:
                self._transport.close()
            self._closed = True

    def fetch(self, *, limit: int = 200) -> dict[str, Any]:
        """Slack → local: fill the inbox gap; heal outbox already on Slack."""
        fetch = getattr(self._transport, "fetch", None)
        if fetch is None:
            return {"key": self.key.format(), "notes": ["transport has no fetch"]}
        return fetch(self.key, limit=limit)

    def flush(
        self,
        *,
        retry_failed: bool = True,
        repair_first: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Local → Slack: drain pending/failed outbox (closes the outbox gap)."""
        flush = getattr(self._transport, "flush", None)
        if flush is None:
            return {"key": self.key.format(), "notes": ["transport has no flush"]}
        return flush(
            self.key,
            retry_failed=retry_failed,
            repair_first=repair_first,
            limit=limit,
        )

    def _try_fetch(self) -> None:
        try:
            self.fetch()
        except Exception as e:
            log.debug("autofetch skipped for %s: %s", self.key, e)

    def _try_flush(self) -> None:
        try:
            self.flush()
        except Exception as e:
            log.debug("autoflush skipped for %s: %s", self.key, e)

    def read(
        self,
        *,
        timeout: float | None = None,
        ack: bool = True,
    ) -> MessageRecord | None:
        """Return next inbox message; advances local cursor when ``ack`` is true.

        When ``autofetch`` is enabled and the consumer is at the local inbox tip,
        runs ``fetch`` once before waiting for a message.
        """
        if self.autofetch and self._store().at_inbox_tip(self.key, self.consumer_id):
            self._try_fetch()
        msg = self._transport.read(
            self.key,
            consumer_id=self.consumer_id,
            timeout=timeout,
            ack=ack,
        )
        if msg is None and timeout is not None and self.raise_on_timeout:
            raise Timeout(f"read timed out after {timeout}s on {self.key}")
        return msg

    def read_at(self, offset: int) -> MessageRecord | None:
        """Absolute inbox offset; does not move the cursor."""
        return self._transport.read_at(self.key, offset)

    def write(self, text: str, *, user_id: str | None = None) -> MessageRecord:
        """Append to outbox (local cursor). With ``autoflush``, drain to Slack."""
        rec = self._transport.write(self.key, text, user_id=user_id)
        if self.autoflush:
            self._try_flush()
            updated = self._store().read_at(self.key, rec.offset, log="outbox")
            if updated is not None:
                return updated
        return rec

    def ack(self, offset: int) -> None:
        self._transport.ack(self.key, self.consumer_id, offset)

    def follow(self, *, ack: bool = True) -> Iterator[MessageRecord]:
        """Yield inbox messages forever (blocking)."""
        while True:
            msg = self.read(timeout=None, ack=ack)
            if msg is not None:
                yield msg

    def history(self) -> list[tuple[LogName, MessageRecord]]:
        """Merged inbox+outbox in chat order (local logs; does not move cursors)."""
        return self._store().iter_chat(self.key)

    def reply_key(self, side: Literal["inbox", "outbox"] = "inbox") -> str | None:
        """Reply/follow stream key from last inbox read or last sent outbox.

        Returns None if there is no suitable record (no cursor / no sent ts).
        """
        store = self._store()
        if side == "outbox":
            rec = store.last_sent_outbox(self.key)
        else:
            rec = store.record_at_inbox_cursor(self.key, self.consumer_id)
        if rec is None:
            return None
        return rec.reply_stream_key()
