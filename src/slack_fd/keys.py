"""Stream key parse/format: thread / dm / app."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from slack_fd.errors import InvalidKey

StreamKind = Literal["thread", "dm", "app"]

_THREAD_RE = re.compile(r"^thread:([A-Z0-9]+):(\d+\.\d+)$")
_DM_RE = re.compile(r"^dm:([A-Z0-9]+)$")
_APP_RE = re.compile(r"^app:([A-Z0-9]+)$")


@dataclass(frozen=True, slots=True)
class StreamKey:
    """Logical conversation address for a stream."""

    kind: StreamKind
    channel_id: str | None = None
    thread_ts: str | None = None
    user_id: str | None = None

    def __str__(self) -> str:
        return self.format()

    def format(self) -> str:
        if self.kind == "thread":
            if not self.channel_id or not self.thread_ts:
                raise InvalidKey("thread key requires channel_id and thread_ts")
            return f"thread:{self.channel_id}:{self.thread_ts}"
        if self.kind == "dm":
            if not self.user_id:
                raise InvalidKey("dm key requires user_id")
            return f"dm:{self.user_id}"
        if self.kind == "app":
            if not self.user_id:
                raise InvalidKey("app key requires user_id")
            return f"app:{self.user_id}"
        raise InvalidKey(f"unknown kind: {self.kind}")

    def encoded_dirname(self) -> str:
        """Filesystem-safe directory name for this key."""
        return self.format().replace(":", "__").replace(".", "_")

    @classmethod
    def parse(cls, value: str) -> StreamKey:
        value = value.strip()
        if not value:
            raise InvalidKey("empty stream key")

        m = _THREAD_RE.match(value)
        if m:
            return cls.thread(m.group(1), m.group(2))

        m = _DM_RE.match(value)
        if m:
            return cls.dm(m.group(1))

        m = _APP_RE.match(value)
        if m:
            return cls.app(m.group(1))

        raise InvalidKey(
            f"invalid stream key {value!r}; expected "
            "thread:<channel>:<ts>, dm:<user>, or app:<user>"
        )

    @classmethod
    def thread(cls, channel_id: str, thread_ts: str) -> StreamKey:
        if not channel_id or not thread_ts:
            raise InvalidKey("thread key requires channel_id and thread_ts")
        return cls(kind="thread", channel_id=channel_id, thread_ts=thread_ts)

    @classmethod
    def dm(cls, user_id: str) -> StreamKey:
        if not user_id:
            raise InvalidKey("dm key requires user_id")
        return cls(kind="dm", user_id=user_id)

    @classmethod
    def app(cls, user_id: str) -> StreamKey:
        if not user_id:
            raise InvalidKey("app key requires user_id")
        return cls(kind="app", user_id=user_id)
