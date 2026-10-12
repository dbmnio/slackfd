"""Message record types for inbox/outbox JSONL."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

Direction = Literal["in", "out"]
OutboxStatus = Literal["pending", "sent", "failed"]


@dataclass(slots=True)
class MessageRecord:
    offset: int
    direction: Direction
    stream: str
    text: str
    ts: str | None = None
    channel: str | None = None
    user: str | None = None
    thread_ts: str | None = None
    status: OutboxStatus | None = None
    client_msg_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Keep JSONL compact: omit nullish optional fields except core ones.
        return {k: v for k, v in data.items() if v is not None and v != {}}

    def reply_stream_key(self) -> str:
        """Stream key to follow/write for replies related to this message.

        - Already on ``thread:…`` → that stream.
        - ``app:`` / ``dm:`` → same IM stream (inbound demux does not use thread:D yet).
        - Else ``thread:{channel}:{thread_ts or ts}`` when channel + ts are known.
        """
        stream = self.stream or ""
        if stream.startswith("thread:"):
            return stream
        if stream.startswith("app:") or stream.startswith("dm:"):
            return stream
        root = self.thread_ts or self.ts
        if self.channel and root:
            return f"thread:{self.channel}:{root}"
        return stream

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageRecord:
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        if "raw" not in filtered:
            filtered["raw"] = {}
        return cls(**filtered)
