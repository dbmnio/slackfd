"""In-process Socket Mode transport (Phase 2)."""

from __future__ import annotations

from slack_fd.keys import StreamKey
from slack_fd.records import MessageRecord


class EmbedTransport:
    def open(self, key: StreamKey) -> None:
        raise NotImplementedError("EmbedTransport arrives in Phase 2")

    def close(self) -> None:
        return None

    def read(
        self,
        key: StreamKey,
        *,
        consumer_id: str,
        timeout: float | None,
        ack: bool,
    ) -> MessageRecord | None:
        raise NotImplementedError("EmbedTransport arrives in Phase 2")

    def read_at(self, key: StreamKey, offset: int) -> MessageRecord | None:
        raise NotImplementedError("EmbedTransport arrives in Phase 2")

    def write(
        self,
        key: StreamKey,
        text: str,
        *,
        user_id: str | None = None,
    ) -> MessageRecord:
        raise NotImplementedError("EmbedTransport arrives in Phase 2")

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        raise NotImplementedError("EmbedTransport arrives in Phase 2")
