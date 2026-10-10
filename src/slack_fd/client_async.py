"""Async SlackStream client (Phase 3 — stub for now)."""

from __future__ import annotations

from slack_fd.keys import StreamKey


class AsyncSlackStream:
    """Async counterpart to SlackStream. Implemented in Phase 3."""

    def __init__(self, key: StreamKey | str, **_kwargs: object) -> None:
        self.key = StreamKey.parse(key) if isinstance(key, str) else key

    async def __aenter__(self) -> AsyncSlackStream:
        raise NotImplementedError("AsyncSlackStream arrives in Phase 3")

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def read(self, **_kwargs: object) -> None:
        raise NotImplementedError("AsyncSlackStream arrives in Phase 3")

    async def write(self, text: str, **_kwargs: object) -> None:
        raise NotImplementedError("AsyncSlackStream arrives in Phase 3")
