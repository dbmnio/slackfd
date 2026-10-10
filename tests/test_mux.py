from slack_fd.daemon.mux import apply_send_result, response_data, send_message
from slack_fd.keys import StreamKey
from slack_fd.records import MessageRecord


class FakeSlackResponse:
    """Mimics slack_sdk SlackResponse: iterating yields keys, not items."""

    def __init__(self, data: dict) -> None:
        self.data = data

    def __iter__(self):
        return iter(self.data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.opens: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return FakeSlackResponse({"ok": True, "ts": "9.9"})

    def conversations_open(self, **kwargs):
        self.opens.append(kwargs)
        return FakeSlackResponse({"ok": True, "channel": {"id": "D999"}})


def test_response_data_handles_slack_response():
    resp = FakeSlackResponse({"ok": True, "ts": "1.2", "error": None})
    # dict(SlackResponse)-style conversion is unreliable; use response_data.
    assert response_data(resp) == {"ok": True, "ts": "1.2", "error": None}
    assert response_data({"ok": False, "error": "x"})["error"] == "x"


def test_send_thread():
    client = FakeClient()
    key = StreamKey.thread("C1", "1.0")
    send_message(client, key, "hi")
    assert client.posts[0]["channel"] == "C1"
    assert client.posts[0]["thread_ts"] == "1.0"
    assert client.posts[0]["text"] == "hi"


def test_send_dm_opens_conversation():
    client = FakeClient()
    key = StreamKey.app("U1")
    send_message(client, key, "yo")
    assert client.opens[0]["users"] == ["U1"]
    assert client.posts[0]["channel"] == "D999"


def test_apply_send_result():
    rec = MessageRecord(offset=0, direction="out", stream="dm:U1", text="x")
    status, ts, err = apply_send_result(rec, {"ok": True, "ts": "1.2"})
    assert status == "sent" and ts == "1.2" and err is None
    status, ts, err = apply_send_result(rec, {"ok": False, "error": "channel_not_found"})
    assert status == "failed" and err == "channel_not_found"
