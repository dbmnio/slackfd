"""Stream key parse/format: ``<channel_id>`` or ``<channel_id>:<thread_ts>``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from slack_fd.errors import InvalidKey

StreamKind = Literal["channel", "thread"]

# Slack conversation ids typically C… / D… / G… (letter + alphanumerics).
_CHANNEL_RE = re.compile(r"^([CDG][A-Z0-9]+)$")
_THREAD_RE = re.compile(r"^([CDG][A-Z0-9]+):(\d+\.\d+)$")


@dataclass(frozen=True, slots=True)
class StreamKey:
    """Slack conversation address: channel id, optional thread parent ts."""

    channel_id: str
    thread_ts: str | None = None

    @property
    def kind(self) -> StreamKind:
        return "thread" if self.thread_ts else "channel"

    def __str__(self) -> str:
        return self.format()

    def format(self) -> str:
        if not self.channel_id:
            raise InvalidKey("channel_id required")
        if self.thread_ts:
            return f"{self.channel_id}:{self.thread_ts}"
        return self.channel_id

    def encoded_dirname(self) -> str:
        """Filesystem-safe directory name for this key."""
        return self.format().replace(":", "__").replace(".", "_")

    @classmethod
    def from_dirname(cls, name: str) -> StreamKey:
        """Inverse of ``encoded_dirname`` (Slack-native keys only)."""
        name = name.strip()
        if not name:
            raise InvalidKey("empty stream dirname")
        if "__" in name:
            channel, ts_enc = name.split("__", 1)
            if "_" not in ts_enc:
                raise InvalidKey(f"invalid stream dirname {name!r}")
            left, right = ts_enc.split("_", 1)
            return cls.thread(channel, f"{left}.{right}")
        return cls.channel(name)

    @classmethod
    def parse(cls, value: str) -> StreamKey:
        value = value.strip()
        if not value:
            raise InvalidKey("empty stream key")

        m = _THREAD_RE.match(value)
        if m:
            return cls.thread(m.group(1), m.group(2))

        m = _CHANNEL_RE.match(value)
        if m:
            return cls.channel(m.group(1))

        raise InvalidKey(
            f"invalid stream key {value!r}; expected "
            "<channel_id> or <channel_id>:<thread_ts> "
            "(e.g. C012ABC, D012ABC, C012ABC:1710000000.000100)"
        )

    @classmethod
    def channel(cls, channel_id: str) -> StreamKey:
        if not channel_id or not _CHANNEL_RE.match(channel_id):
            raise InvalidKey(
                f"invalid channel_id {channel_id!r}; expected C…/D…/G… id"
            )
        return cls(channel_id=channel_id, thread_ts=None)

    @classmethod
    def thread(cls, channel_id: str, thread_ts: str) -> StreamKey:
        if not channel_id or not _CHANNEL_RE.match(channel_id):
            raise InvalidKey(
                f"invalid channel_id {channel_id!r}; expected C…/D…/G… id"
            )
        if not thread_ts or not re.match(r"^\d+\.\d+$", thread_ts):
            raise InvalidKey(f"invalid thread_ts {thread_ts!r}")
        return cls(channel_id=channel_id, thread_ts=thread_ts)
