"""Send outbox messages via Slack Web API."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol

from slack_fd.keys import StreamKey
from slack_fd.records import MessageRecord

log = logging.getLogger("slack_fd.mux")


class WebApi(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...


def response_data(resp: Any) -> dict[str, Any]:
    """Normalize slack_sdk SlackResponse / dict into a plain dict.

    Do not call ``dict(slack_response)`` — it iterates keys and breaks.
    """
    if isinstance(resp, dict):
        return resp
    data = getattr(resp, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(resp, Mapping):
        return {
            "ok": resp.get("ok"),
            "ts": resp.get("ts"),
            "error": resp.get("error"),
            "channel": resp.get("channel"),
        }
    return {"ok": False, "error": "unreadable_response"}


def send_message(client: WebApi, key: StreamKey, text: str) -> dict[str, Any]:
    """Post ``text`` to the Slack destination for ``key``. Returns API response data."""
    kwargs: dict[str, Any] = {
        "channel": key.channel_id,
        "text": text,
    }
    if key.thread_ts:
        kwargs["thread_ts"] = key.thread_ts
    return response_data(client.chat_postMessage(**kwargs))


def apply_send_result(
    record: MessageRecord,
    response: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """Return (status, ts, error) from a chat.postMessage-like response."""
    if response.get("ok"):
        return "sent", response.get("ts"), None
    err = str(response.get("error") or "send_failed")
    return "failed", None, err
