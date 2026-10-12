from slack_fd.flush import flush_stream
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord


class FakeFlushClient:
    def __init__(self, history: list[dict]) -> None:
        self.history = history
        self.posts: list[dict] = []

    def auth_test(self, **kwargs):
        return {"ok": True, "user_id": "U_BOT"}

    def conversations_open(self, **kwargs):
        return {"ok": True, "channel": {"id": "D1"}}

    def conversations_history(self, **kwargs):
        return {"ok": True, "messages": list(self.history)}

    def conversations_replies(self, **kwargs):
        return {"ok": True, "messages": list(self.history)}

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"9.{len(self.posts)}"}


def test_flush_default_sends_without_history_repair(tmp_path):
    """Hot path: repair_first=False (default) — send only, no history HTTP."""
    key = StreamKey.channel("DUSER")
    store = LogStore(tmp_path)
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="a",
            status="pending",
        ),
        log="outbox",
    )
    client = FakeFlushClient([])
    result = flush_stream(key, data_dir=tmp_path, client=client, store=store)
    assert result.repaired == 0
    assert result.sent == 1
    assert len(client.posts) == 1
    assert store.read_at(key, 0, log="outbox").status == "sent"


def test_flush_repairs_then_sends_remaining(tmp_path):
    """Admin/legacy: repair_first=True matches history then sends the rest."""
    key = StreamKey.channel("DUSER")
    store = LogStore(tmp_path)
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="already-on-slack",
            status="pending",
        ),
        log="outbox",
    )
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="never-sent",
            status="pending",
        ),
        log="outbox",
    )
    history = [
        {"type": "message", "user": "U_BOT", "text": "already-on-slack", "ts": "1.0"},
    ]
    client = FakeFlushClient(history)
    result = flush_stream(
        key, data_dir=tmp_path, client=client, store=store, repair_first=True
    )
    assert result.repaired == 1
    assert result.sent == 1
    assert len(client.posts) == 1
    assert client.posts[0]["text"] == "never-sent"
    assert store.read_at(key, 0, log="outbox").status == "sent"
    assert store.read_at(key, 1, log="outbox").status == "sent"
