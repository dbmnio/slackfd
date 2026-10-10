"""Synchronous SlackStream client."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from slack_fd.errors import DaemonUnavailable, Timeout
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogName, LogStore
from slack_fd.records import MessageRecord
from slack_fd.transport.base import Transport
from slack_fd.transport.daemon import DaemonTransport
from slack_fd.transport.embed import EmbedTransport
from slack_fd.transport.fake import FakeTransport

TransportName = Literal["fake", "daemon", "embed", "auto"]


class SlackStream:
    """File-descriptor-like sync handle for one conversation stream."""

    def __init__(
        self,
        key: StreamKey | str,
        *,
        consumer_id: str = "default",
        transport: TransportName | Transport = "auto",
        data_dir: Path | None = None,
        raise_on_timeout: bool = False,
    ) -> None:
        self.key = StreamKey.parse(key) if isinstance(key, str) else key
        self.consumer_id = consumer_id
        self.raise_on_timeout = raise_on_timeout
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

    def read(
        self,
        *,
        timeout: float | None = None,
        ack: bool = True,
    ) -> MessageRecord | None:
        """Block until the next unread inbox message (or timeout)."""
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
        return self._transport.write(self.key, text, user_id=user_id)

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
        return LogStore(self._data_dir).iter_chat(self.key)
