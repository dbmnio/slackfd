from pathlib import Path

from slack_fd.daemon.service import DaemonService
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord


def test_append_inbox_skips_duplicate_ts(tmp_path: Path):
    svc = DaemonService(tmp_path)
    key = StreamKey.channel("C1")
    event = {
        "type": "message",
        "channel": "C1",
        "user": "U1",
        "text": "hello",
        "ts": "1.0",
    }
    first = svc.ingest_slack_event(event, bot_user_id="UBOT")
    second = svc.ingest_slack_event(event, bot_user_id="UBOT")
    assert len(first) == 1
    assert second == []
    assert svc.store.inbox_len(key) == 1


def test_fetch_does_not_duplicate_live_demux(tmp_path: Path, monkeypatch):
    from slack_fd.fetch import fetch_stream

    store = LogStore(tmp_path)
    key = StreamKey.thread("C1", "1.0")
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="in",
            stream="",
            text="parent",
            ts="1.0",
            channel="C1",
            user="U1",
            thread_ts="1.0",
        ),
        log="inbox",
    )

    class Client:
        def auth_test(self, **kwargs):
            return {"ok": True, "user_id": "UBOT"}

        def conversations_replies(self, **kwargs):
            return {
                "ok": True,
                "messages": [
                    {"ts": "1.0", "text": "parent", "user": "U1", "thread_ts": "1.0"},
                    {"ts": "1.1", "text": "reply", "user": "U1", "thread_ts": "1.0"},
                ],
            }

    result = fetch_stream(key, data_dir=tmp_path, client=Client(), store=store)
    assert result.inbound_added == 1  # only the new reply
    assert store.inbox_len(key) == 2
    assert store.inbox_has_ts(key, "1.0")
    assert store.inbox_has_ts(key, "1.1")
