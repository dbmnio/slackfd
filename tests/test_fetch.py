from slack_fd.fetch import fetch_stream
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord


class FakeFetchClient:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.opens: list[dict] = []

    def auth_test(self, **kwargs):
        return {"ok": True, "user_id": "U_BOT"}

    def conversations_open(self, **kwargs):
        self.opens.append(kwargs)
        return {"ok": True, "channel": {"id": "D999"}}

    def conversations_history(self, **kwargs):
        return {"ok": True, "messages": list(self._messages)}

    def conversations_replies(self, **kwargs):
        return {"ok": True, "messages": list(self._messages)}


def test_fetch_adds_inbound_and_repairs_outbox(tmp_path):
    key = StreamKey.app("U_USER")
    store = LogStore(tmp_path)
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="hello from sfd",
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
            text="2 hello from sfd",
            status="failed",
            error="boom",
        ),
        log="outbox",
    )

    messages = [
        {"type": "message", "user": "U_USER", "text": "erpy", "ts": "3.0"},
        {"type": "message", "user": "U_BOT", "text": "2 hello from sfd", "ts": "2.0"},
        {"type": "message", "user": "U_BOT", "text": "hello from sfd", "ts": "1.5"},
        {"type": "message", "user": "U_USER", "text": "hi again", "ts": "1.0"},
    ]
    result = fetch_stream(
        key,
        data_dir=tmp_path,
        client=FakeFetchClient(messages),
        store=store,
    )
    assert result.inbound_added == 2
    assert result.outbox_repaired == 2

    chat = store.iter_chat(key)
    assert [r.text for _, r in chat] == [
        "hi again",
        "hello from sfd",
        "2 hello from sfd",
        "erpy",
    ]
    out0 = store.read_at(key, 0, log="outbox")
    assert out0 is not None
    assert out0.status == "sent" and out0.ts == "1.5"
    assert out0.error is None


def test_fetch_skips_existing_inbox_ts(tmp_path):
    key = StreamKey.dm("U_USER")
    store = LogStore(tmp_path)
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="in",
            stream="",
            text="hi again",
            ts="1.0",
            user="U_USER",
        ),
    )
    messages = [
        {"type": "message", "user": "U_USER", "text": "hi again", "ts": "1.0"},
        {"type": "message", "user": "U_USER", "text": "new", "ts": "2.0"},
    ]
    result = fetch_stream(
        key,
        data_dir=tmp_path,
        client=FakeFetchClient(messages),
        store=store,
    )
    assert result.inbound_added == 1
    assert store.read_at(key, 1).text == "new"
