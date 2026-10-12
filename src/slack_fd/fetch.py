"""Fetch (Slack → local): Slack history → local inbox; repair outbox from bot messages.

Queues remain local-first. This does not move read cursors or rewrite history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from slack_fd.config import load_config
from slack_fd.daemon.mux import response_data
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord

log = logging.getLogger("slack_fd.fetch")

_SKIP_SUBTYPES = {
    "message_changed",
    "message_deleted",
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "message_replied",
}


class FetchWebApi(Protocol):
    def auth_test(self, **kwargs: Any) -> Any: ...

    def conversations_open(self, **kwargs: Any) -> Any: ...

    def conversations_history(self, **kwargs: Any) -> Any: ...

    def conversations_replies(self, **kwargs: Any) -> Any: ...


@dataclass
class FetchResult:
    key: str
    channel: str
    bot_user_id: str
    fetched: int = 0
    inbound_added: int = 0
    outbox_repaired: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "channel": self.channel,
            "bot_user_id": self.bot_user_id,
            "fetched": self.fetched,
            "inbound_added": self.inbound_added,
            "outbox_repaired": self.outbox_repaired,
            "skipped": self.skipped,
            "notes": self.notes,
        }


def fetch_stream(
    key: StreamKey | str,
    *,
    data_dir: Path | None = None,
    client: FetchWebApi | None = None,
    store: LogStore | None = None,
    limit: int = 200,
) -> FetchResult:
    """Pull Slack history for ``key`` and reconcile local logs."""
    stream = StreamKey.parse(key) if isinstance(key, str) else key
    root = (data_dir or load_config().data_dir).resolve()
    store = store or LogStore(root)
    store.stream_dir(stream)

    if client is None:
        client = _default_client(root)

    auth = response_data(client.auth_test())
    if not auth.get("ok"):
        raise RuntimeError(f"auth.test failed: {auth.get('error')}")
    bot_user_id = str(auth.get("user_id") or "")

    channel = resolve_channel(client, stream)
    messages = fetch_messages(client, stream, channel, limit=limit)

    result = FetchResult(
        key=stream.format(),
        channel=channel,
        bot_user_id=bot_user_id,
        fetched=len(messages),
    )

    existing_inbox_ts = {
        r.ts for r in store.iter_from(stream, 0, log="inbox") if r.ts
    }
    existing_out_ts = {
        r.ts for r in store.iter_from(stream, 0, log="outbox") if r.ts
    }

    inbound_candidates: list[dict[str, Any]] = []
    bot_candidates: list[dict[str, Any]] = []

    for msg in messages:
        if not _usable_message(msg):
            result.skipped += 1
            continue
        if _is_bot_message(msg, bot_user_id):
            bot_candidates.append(msg)
        else:
            inbound_candidates.append(msg)

    # History is newest-first from Slack; append older first for nicer offsets.
    for msg in sorted(inbound_candidates, key=lambda m: float(m["ts"])):
        ts = str(msg["ts"])
        if ts in existing_inbox_ts:
            continue
        if stream.kind == "thread":
            # Only messages in this thread (parent or replies).
            root_ts = stream.thread_ts
            thread_ts = msg.get("thread_ts") or msg.get("ts")
            if str(thread_ts) != root_ts and str(msg.get("ts")) != root_ts:
                continue
        else:
            # Channel / IM tape: top-level only (skip replies that belong in C:ts streams).
            tts = msg.get("thread_ts")
            if tts and str(tts) != str(msg.get("ts")):
                continue
        # Re-check on disk: a live demux may have appended this ts since we
        # snapped existing_inbox_ts at the start of fetch.
        if store.inbox_has_ts(stream, ts):
            existing_inbox_ts.add(ts)
            continue
        rec = MessageRecord(
            offset=-1,
            direction="in",
            stream=stream.format(),
            text=str(msg.get("text") or ""),
            ts=ts,
            channel=channel,
            user=msg.get("user"),
            thread_ts=msg.get("thread_ts"),
            raw=_slim(msg),
        )
        store.append(stream, rec, log="inbox")
        existing_inbox_ts.add(ts)
        result.inbound_added += 1

    result.outbox_repaired = repair_outbox(
        store,
        stream,
        bot_messages=bot_candidates,
        already_used_ts=existing_out_ts,
    )
    return result


def resolve_channel(client: FetchWebApi, key: StreamKey) -> str:
    del client  # channel id is native on the key; no open/lookup
    if not key.channel_id:
        raise ValueError("stream key missing channel_id")
    return key.channel_id


def fetch_messages(
    client: FetchWebApi,
    key: StreamKey,
    channel: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if key.kind == "thread":
        assert key.thread_ts
        data = response_data(
            client.conversations_replies(
                channel=channel,
                ts=key.thread_ts,
                limit=min(limit, 1000),
            )
        )
        if not data.get("ok"):
            raise RuntimeError(f"conversations.replies failed: {data.get('error')}")
        return list(data.get("messages") or [])

    out: list[dict[str, Any]] = []
    cursor: str | None = None
    remaining = limit
    while remaining > 0:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "limit": min(remaining, 200),
        }
        if cursor:
            kwargs["cursor"] = cursor
        data = response_data(client.conversations_history(**kwargs))
        if not data.get("ok"):
            raise RuntimeError(f"conversations.history failed: {data.get('error')}")
        batch = list(data.get("messages") or [])
        out.extend(batch)
        remaining -= len(batch)
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor or not batch:
            break
    return out


def repair_outbox(
    store: LogStore,
    key: StreamKey,
    *,
    bot_messages: list[dict[str, Any]],
    already_used_ts: set[str],
) -> int:
    """Match pending/failed outbox rows to bot history messages by text order."""
    available = [
        m
        for m in sorted(bot_messages, key=lambda m: float(m["ts"]))
        if str(m["ts"]) not in already_used_ts and m.get("text") is not None
    ]
    repaired = 0
    for rec in list(store.iter_from(key, 0, log="outbox")):
        needs = rec.status in {"pending", "failed"} or not rec.ts
        if not needs:
            continue
        match_idx = next(
            (i for i, m in enumerate(available) if str(m.get("text")) == rec.text),
            None,
        )
        if match_idx is None:
            continue
        matched = available.pop(match_idx)
        ts = str(matched["ts"])
        store.update_outbox_status(key, rec.offset, "sent", ts=ts, error=None)
        already_used_ts.add(ts)
        repaired += 1
        log.info("repaired outbox offset=%s ts=%s text=%r", rec.offset, ts, rec.text[:60])
    return repaired


def _default_client(data_dir: Path) -> FetchWebApi:
    cfg = load_config(data_dir)
    if not cfg.bot_token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN required for fetch (app token not needed for history)"
        )
    from slack_sdk import WebClient

    return WebClient(token=cfg.bot_token)


def _usable_message(msg: dict[str, Any]) -> bool:
    if msg.get("subtype") in _SKIP_SUBTYPES:
        return False
    if not msg.get("ts"):
        return False
    if msg.get("text") is None:
        return False
    return True


def _is_bot_message(msg: dict[str, Any], bot_user_id: str) -> bool:
    if bot_user_id and msg.get("user") == bot_user_id:
        return True
    if msg.get("subtype") == "bot_message":
        return True
    if msg.get("bot_id") and bot_user_id and msg.get("user") == bot_user_id:
        return True
    return False


def _slim(msg: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "type",
        "subtype",
        "user",
        "bot_id",
        "ts",
        "thread_ts",
        "client_msg_id",
        "team",
    )
    return {k: msg[k] for k in keep if k in msg}


# Back-compat aliases (old name "sync" felt bidirectional).
SyncWebApi = FetchWebApi
SyncResult = FetchResult


def sync_stream(*args, **kwargs) -> FetchResult:
    """Deprecated alias for :func:`fetch_stream`."""
    return fetch_stream(*args, **kwargs)
