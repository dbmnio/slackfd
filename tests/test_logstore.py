from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord


def test_append_and_read_at(tmp_path):
    store = LogStore(tmp_path)
    key = StreamKey.thread("C1", "1.0")
    a = store.append(
        key,
        MessageRecord(offset=-1, direction="in", stream="", text="hi"),
    )
    b = store.append(
        key,
        MessageRecord(offset=-1, direction="in", stream="", text="yo"),
    )
    assert a.offset == 0
    assert b.offset == 1
    assert store.read_at(key, 0).text == "hi"
    assert store.read_at(key, 1).text == "yo"
    assert store.read_at(key, 2) is None


def test_cursor_ack(tmp_path):
    store = LogStore(tmp_path)
    key = StreamKey.dm("U1")
    store.append(key, MessageRecord(offset=-1, direction="in", stream="", text="a"))
    store.append(key, MessageRecord(offset=-1, direction="in", stream="", text="b"))

    assert store.get_cursor(key, "default") == -1
    nxt = store.next_after_cursor(key, "default")
    assert nxt is not None and nxt.text == "a"
    store.ack(key, "default", nxt.offset)
    nxt2 = store.next_after_cursor(key, "default")
    assert nxt2 is not None and nxt2.text == "b"


def test_outbox_status(tmp_path):
    store = LogStore(tmp_path)
    key = StreamKey.app("U9")
    rec = store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="ping",
            status="pending",
        ),
        log="outbox",
    )
    updated = store.update_outbox_status(key, rec.offset, "sent", ts="1.2")
    assert updated is not None
    assert updated.status == "sent"
    assert updated.ts == "1.2"
