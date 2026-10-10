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


def test_iter_chat_merges_by_ts(tmp_path):
    store = LogStore(tmp_path)
    key = StreamKey.app("U1")
    store.append(
        key,
        MessageRecord(offset=-1, direction="in", stream="", text="second", ts="2.0"),
    )
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="first-reply-earlier",
            ts="1.5",
            status="sent",
        ),
        log="outbox",
    )
    store.append(
        key,
        MessageRecord(offset=-1, direction="in", stream="", text="first", ts="1.0"),
    )
    chat = store.iter_chat(key)
    assert [r.text for _, r in chat] == ["first", "first-reply-earlier", "second"]


def test_trim_keeps_newest_and_adjusts_cursor(tmp_path):
    store = LogStore(tmp_path)
    key = StreamKey.dm("U2")
    for i in range(5):
        store.append(
            key,
            MessageRecord(offset=-1, direction="in", stream="", text=f"m{i}", ts=str(float(i))),
        )
    store.ack(key, "default", 3)  # cursor at 3
    result = store.trim(key, inbox_keep=2)
    assert result["inbox_dropped"] == 3
    assert store.read_at(key, 0).text == "m3"
    assert store.read_at(key, 1).text == "m4"
    assert store.get_cursor(key, "default") == 0  # 3 - 3
