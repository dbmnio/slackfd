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
    channel_type = event.get("channel_type")  # im / channel / group / mpim

    keys: list[StreamKey] = []

    if channel_type == "im" or (isinstance(channel, str) and channel.startswith("D")):
        if not user:
            return None
        # One stream per IM. Prefer app:<user> (user → bot). Writing via dm:<user>
        # still targets the same Slack IM; we do not dual-append inbound to both
        # (that looked like duplicate cursors / messages in logs).
        keys.append(StreamKey.app(user))
    else:
        # Channel / group / mpim: thread stream (top-level message is its own root)
        root = thread_ts or ts
        keys.append(StreamKey.thread(channel, root))

    return DemuxedMessage(
        keys=tuple(keys),
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
