"""Async SlackStream client — thin asyncio wrapper over the sync client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from slack_fd.client_sync import SlackStream, TransportName
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogName
from slack_fd.records import MessageRecord
from slack_fd.transport.base import Transport


class AsyncSlackStream:
    """Async counterpart to SlackStream (daemon I/O via ``asyncio.to_thread``)."""

    def __init__(
        self,
        key: StreamKey | str,
        *,
        consumer_id: str = "default",
        transport: TransportName | Transport = "auto",
        data_dir: Path | None = None,
        raise_on_timeout: bool = False,
    ) -> None:
        self._sync = SlackStream(
            key,
            consumer_id=consumer_id,
            transport=transport,
            data_dir=data_dir,
            raise_on_timeout=raise_on_timeout,
        )
        self.key = self._sync.key
        self.consumer_id = self._sync.consumer_id

    async def __aenter__(self) -> AsyncSlackStream:
        await asyncio.to_thread(self._sync.__enter__)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self._sync.close)

    async def read(
        self,
        *,
        timeout: float | None = None,
        ack: bool = True,
    ) -> MessageRecord | None:
        return await asyncio.to_thread(self._sync.read, timeout=timeout, ack=ack)

    async def read_at(self, offset: int) -> MessageRecord | None:
        return await asyncio.to_thread(self._sync.read_at, offset)

    async def write(self, text: str, *, user_id: str | None = None) -> MessageRecord:
        return await asyncio.to_thread(self._sync.write, text, user_id=user_id)

    async def ack(self, offset: int) -> None:
        await asyncio.to_thread(self._sync.ack, offset)

    async def history(self) -> list[tuple[LogName, MessageRecord]]:
        return await asyncio.to_thread(self._sync.history)

    async def follow(self, *, ack: bool = True) -> AsyncIterator[MessageRecord]:
        while True:
            msg = await self.read(timeout=None, ack=ack)
            if msg is not None:
                yield msg
