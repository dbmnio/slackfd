"""Flush outbox: send pending/failed local messages to Slack.

Distinct from ``sync`` (Slack → local). Flush is local → Slack.

By default, runs a history-based outbox *repair* first so messages that already
exist in Slack (e.g. status stuck at pending) are marked sent and not duplicated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slack_fd.config import load_config
from slack_fd.daemon.mux import apply_send_result, response_data, send_message
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.sync import SyncWebApi, fetch_messages, repair_outbox, resolve_channel

log = logging.getLogger("slack_fd.flush")


@dataclass
class FlushResult:
    key: str
    repaired: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "repaired": self.repaired,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "notes": self.notes,
        }


def flush_stream(
    key: StreamKey | str,
    *,
    data_dir: Path | None = None,
    client: SyncWebApi | None = None,
    store: LogStore | None = None,
    retry_failed: bool = True,
    repair_first: bool = True,
    limit: int = 200,
) -> FlushResult:
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

    result = FlushResult(key=stream.format())

    if repair_first:
        channel = resolve_channel(client, stream)
        messages = fetch_messages(client, stream, channel, limit=limit)
        bot_msgs = [
            m
            for m in messages
            if m.get("ts")
            and m.get("text") is not None
            and (
                (bot_user_id and m.get("user") == bot_user_id)
                or m.get("subtype") == "bot_message"
            )
        ]
        used = {r.ts for r in store.iter_from(stream, 0, log="outbox") if r.ts}
        result.repaired = repair_outbox(
            store, stream, bot_messages=bot_msgs, already_used_ts=used
        )
        if result.repaired:
            result.notes.append(
                f"repaired {result.repaired} outbox row(s) from Slack history before send"
            )

    for rec in list(store.iter_from(stream, 0, log="outbox")):
        if rec.status == "sent" and rec.ts:
            result.skipped += 1
            continue
        if rec.status == "failed" and not retry_failed:
            result.skipped += 1
            continue
        if rec.status not in {"pending", "failed"} and rec.ts:
            result.skipped += 1
            continue

        try:
            data = send_message(client, stream, rec.text)
            status, ts, error = apply_send_result(rec, data)
            store.update_outbox_status(
                stream, rec.offset, status, ts=ts, error=error  # type: ignore[arg-type]
            )
            if status == "sent":
                result.sent += 1
                log.info("flushed outbox offset=%s ts=%s", rec.offset, ts)
            else:
                result.failed += 1
                log.warning("flush send failed offset=%s error=%s", rec.offset, error)
        except Exception as e:
            store.update_outbox_status(stream, rec.offset, "failed", error=str(e))
            result.failed += 1
            log.exception("flush exception offset=%s", rec.offset)

    return result


def _default_client(data_dir: Path) -> SyncWebApi:
    cfg = load_config(data_dir)
    if not cfg.bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN required for flush")
    from slack_sdk import WebClient

    return WebClient(token=cfg.bot_token)
