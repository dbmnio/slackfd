"""Daemon owns send-on-write when Slack is connected."""

from pathlib import Path

from slack_fd.daemon.service import DaemonService
from slack_fd.keys import StreamKey


class _FakeSlack:
    connected = True
    bot_user_id = "U_BOT"

    def __init__(self, client: object) -> None:
        self.client = client


class _SendClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def auth_test(self, **kwargs):
        return {"ok": True, "user_id": "U_BOT"}

    def conversations_history(self, **kwargs):
        return {"ok": True, "messages": []}

    def conversations_replies(self, **kwargs):
        return {"ok": True, "messages": []}

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"9.{len(self.posts)}"}


def test_daemon_write_sends_without_client_flush(tmp_path: Path):
    svc = DaemonService(tmp_path)
    client = _SendClient()
    svc.slack = _FakeSlack(client)  # type: ignore[assignment]
    key = StreamKey.channel("D1")
    rec = svc.write(key, "hello from daemon")
    assert rec.status == "sent"
    assert rec.ts == "9.1"
    assert len(client.posts) == 1
    assert client.posts[0]["text"] == "hello from daemon"
    assert client.posts[0]["channel"] == "D1"
