from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord
from slack_fd.cli import main


def test_reply_stream_key_thread_unchanged():
    rec = MessageRecord(
        offset=0,
        direction="in",
        stream="thread:C1:1.0",
        text="hi",
        channel="C1",
        ts="1.1",
        thread_ts="1.0",
    )
    assert rec.reply_stream_key() == "thread:C1:1.0"


def test_reply_stream_key_app_stays_on_app():
    rec = MessageRecord(
        offset=0,
        direction="in",
        stream="app:U1",
        text="hi",
        channel="D1",
        ts="2.0",
        user="U1",
    )
    assert rec.reply_stream_key() == "app:U1"


def test_reply_stream_key_builds_thread_from_channel_ts():
    rec = MessageRecord(
        offset=0,
        direction="in",
        stream="other:x",
        text="hi",
        channel="C9",
        ts="9.0",
    )
    assert rec.reply_stream_key() == "thread:C9:9.0"


def test_record_at_inbox_cursor_and_last_sent(tmp_path):
    key = StreamKey.app("U1")
    store = LogStore(tmp_path)
    store.append(
        key,
        MessageRecord(offset=-1, direction="in", stream="", text="a", ts="1.0", channel="D1"),
        log="inbox",
    )
    store.append(
        key,
        MessageRecord(offset=-1, direction="in", stream="", text="b", ts="2.0", channel="D1"),
        log="inbox",
    )
    assert store.record_at_inbox_cursor(key, "default") is None
    store.ack(key, "default", 0)
    assert store.record_at_inbox_cursor(key, "default").text == "a"
    store.ack(key, "default", 1)
    assert store.record_at_inbox_cursor(key, "default").text == "b"

    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="pending",
            status="pending",
        ),
        log="outbox",
    )
    assert store.last_sent_outbox(key) is None
    store.append(
        key,
        MessageRecord(
            offset=-1,
            direction="out",
            stream="",
            text="sent",
            status="sent",
            ts="3.0",
            channel="D1",
        ),
        log="outbox",
    )
    assert store.last_sent_outbox(key).text == "sent"


def test_cli_key_inbox_and_outbox(tmp_path, capsys):
    key = "app:U9"
    store = LogStore(tmp_path)
    sk = StreamKey.parse(key)
    store.append(
        sk,
        MessageRecord(
            offset=-1,
            direction="in",
            stream=key,
            text="hello",
            ts="1.0",
            channel="D9",
            user="U9",
        ),
        log="inbox",
    )
    store.ack(sk, "default", 0)
    store.append(
        sk,
        MessageRecord(
            offset=-1,
            direction="out",
            stream=key,
            text="reply",
            status="sent",
            ts="2.0",
            channel="D9",
        ),
        log="outbox",
    )

    try:
        main(["--data-dir", str(tmp_path), "key", key])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == "app:U9"

    try:
        main(["--data-dir", str(tmp_path), "key", "-o", key])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == "app:U9"


def test_cli_key_outbox_pending_fails(tmp_path, capsys):
    key = "thread:C1:1.0"
    store = LogStore(tmp_path)
    sk = StreamKey.parse(key)
    store.append(
        sk,
        MessageRecord(
            offset=-1,
            direction="out",
            stream=key,
            text="x",
            status="pending",
        ),
        log="outbox",
    )
    try:
        main(["--data-dir", str(tmp_path), "key", "-o", key])
    except SystemExit as e:
        assert e.code == 1
    err = capsys.readouterr().err
    assert "no sent outbox" in err
