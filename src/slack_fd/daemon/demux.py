"""Map Slack message events → stream keys + inbox fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from slack_fd.keys import StreamKey


@dataclass(frozen=True, slots=True)
class DemuxedMessage:
    keys: tuple[StreamKey, ...]
    text: str
    ts: str
    channel: str
    user: str | None
    thread_ts: str | None
    raw: dict[str, Any]


def demux_message_event(
    event: dict[str, Any],
    *,
    bot_user_id: str | None,
) -> DemuxedMessage | None:
    """Return stream target(s) for a Slack ``message`` event, or None to ignore."""
    if event.get("type") != "message":
        return None

    subtype = event.get("subtype")
    if subtype in {
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "message_replied",
    }:
        return None

    # Ignore our own bot traffic to avoid echo loops.
    if bot_user_id and event.get("user") == bot_user_id:
        return None
    if bot_user_id and event.get("bot_id") and event.get("user") == bot_user_id:
        return None
    if subtype == "bot_message" and bot_user_id and event.get("user") == bot_user_id:
        return None

    text = event.get("text")
    if text is None:
        return None
    text = str(text)

    channel = event.get("channel")
    ts = event.get("ts")
    if not channel or not ts:
        return None

    user = event.get("user")
    thread_ts = event.get("thread_ts")

    # One stream per Slack conversation slice:
    # - channel / IM top-level → <channel_id>
    # - thread reply → <channel_id>:<thread_ts>
    if thread_ts and str(thread_ts) != str(ts):
        key = StreamKey.thread(str(channel), str(thread_ts))
    else:
        key = StreamKey.channel(str(channel))

    return DemuxedMessage(
        keys=(key,),
        text=text,
        ts=str(ts),
        channel=str(channel),
        user=str(user) if user else None,
        thread_ts=str(thread_ts) if thread_ts else None,
        raw=_slim_raw(event),
    )


def _slim_raw(event: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "type",
        "subtype",
        "channel",
        "channel_type",
        "user",
        "ts",
        "thread_ts",
        "team",
        "client_msg_id",
    )
    return {k: event[k] for k in keep if k in event}
