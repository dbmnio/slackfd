"""Transport protocol for stream I/O backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from slack_fd.keys import StreamKey
from slack_fd.records import MessageRecord


@runtime_checkable
class Transport(Protocol):
    def open(self, key: StreamKey) -> None: ...

    def close(self) -> None: ...

    def read(
        self,
        key: StreamKey,
        *,
        consumer_id: str,
        timeout: float | None,
        ack: bool,
    ) -> MessageRecord | None: ...

    def read_at(self, key: StreamKey, offset: int) -> MessageRecord | None: ...

    def write(
        self,
        key: StreamKey,
        text: str,
        *,
        user_id: str | None = None,
    ) -> MessageRecord: ...

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None: ...
