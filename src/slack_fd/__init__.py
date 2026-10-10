"""Thin file-descriptor-like streams over Slack conversations."""

from slack_fd.client_sync import SlackStream
from slack_fd.errors import (
    DaemonUnavailable,
    InvalidKey,
    SendFailed,
    SlackFdError,
    Timeout,
)
from slack_fd.keys import StreamKey
from slack_fd.records import MessageRecord

__all__ = [
    "AsyncSlackStream",
    "DaemonUnavailable",
    "InvalidKey",
    "MessageRecord",
    "SendFailed",
    "SlackFdError",
    "SlackStream",
    "StreamKey",
    "Timeout",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    if name == "AsyncSlackStream":
        from slack_fd.client_async import AsyncSlackStream

        return AsyncSlackStream
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
