"""WS-daemon simple API: no autofetch; autoflush for fake; opt-in fetch."""

from slack_fd import SlackStream, StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord
from slack_fd.transport.fake import FakeTransport


def test_write_autoflush_marks_sent(tmp_path):
    key = StreamKey.channel("D1")
    transport = FakeTransport(tmp_path)
    with SlackStream(key, transport=transport) as s:
        out = s.write("hello")
    assert out.status == "sent"
    assert out.ts


def test_write_no_autoflush_stays_pending(tmp_path):
    key = StreamKey.channel("D1")
    transport = FakeTransport(tmp_path)
    with SlackStream(key, transport=transport, autoflush=False) as s:
        out = s.write("hello")
    assert out.status == "pending"
    assert out.ts is None


def test_explicit_flush_after_advanced_write(tmp_path):
    key = StreamKey.channel("D2")
    transport = FakeTransport(tmp_path)
    with SlackStream(key, transport=transport, autoflush=False) as s:
        s.write("a")
        s.write("b")
        result = s.flush()
    assert result["sent"] == 2
    store = LogStore(tmp_path)
    assert store.read_at(key, 0, log="outbox").status == "sent"
    assert store.read_at(key, 1, log="outbox").status == "sent"


def test_read_default_does_not_autofetch(tmp_path):
    key = StreamKey.channel("D9")
    transport = FakeTransport(tmp_path)
    calls: list[str] = []

    def wrapped(k, *, limit=200):
        calls.append("fetch")
        return {"key": k.format()}

    transport.fetch = wrapped  # type: ignore[method-assign]
    with SlackStream(key, transport=transport) as s:
        msg = s.read(timeout=0.1)
    assert msg is None
    assert calls == []


def test_read_opt_in_autofetch_when_at_tip(tmp_path):
    key = StreamKey.thread("C1", "1.0")
    transport = FakeTransport(tmp_path)
    calls: list[str] = []
    orig = transport.fetch

    def wrapped(k, *, limit=200):
        calls.append("fetch")
        transport.inject_inbound(key, "from-fetch")
        return orig(k, limit=limit)

    transport.fetch = wrapped  # type: ignore[method-assign]
    with SlackStream(key, transport=transport, autofetch=True) as s:
        msg = s.read(timeout=0.5)
    assert calls == ["fetch"]
    assert msg is not None and msg.text == "from-fetch"


def test_at_inbox_tip_helper(tmp_path):
    key = StreamKey.channel("D3")
    store = LogStore(tmp_path)
    assert store.at_inbox_tip(key, "default")
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="in",
            stream=key.format(),
            text="x",
            ts="1.0",
        ),
        log="inbox",
    )
    assert not store.at_inbox_tip(key, "default")
    store.ack(key, "default", 0)
    assert store.at_inbox_tip(key, "default")
