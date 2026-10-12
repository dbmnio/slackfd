from slack_fd.daemon.demux import demux_message_event


def test_demux_im_to_app_only():
    event = {
        "type": "message",
        "channel": "D012",
        "channel_type": "im",
        "user": "U999",
        "text": "hello bot",
        "ts": "1710000001.000100",
    }
    d = demux_message_event(event, bot_user_id="U_BOT")
    assert d is not None
    assert [k.format() for k in d.keys] == ["app:U999"]
    assert d.text == "hello bot"


def test_demux_ignores_own_bot_user():
    event = {
        "type": "message",
        "channel": "D012",
        "channel_type": "im",
        "user": "U_BOT",
        "text": "echo",
        "ts": "1.2",
    }
    assert demux_message_event(event, bot_user_id="U_BOT") is None


def test_demux_channel_top_level_as_thread_root():
    event = {
        "type": "message",
        "channel": "C012",
        "channel_type": "channel",
        "user": "U1",
        "text": "hi",
        "ts": "1710000000.000100",
    }
    d = demux_message_event(event, bot_user_id="U_BOT")
    assert d is not None
    assert d.keys[0].format() == "thread:C012:1710000000.000100"


def test_demux_channel_reply():
    event = {
        "type": "message",
        "channel": "C012",
        "channel_type": "channel",
        "user": "U1",
        "text": "reply",
        "ts": "1710000001.000200",
        "thread_ts": "1710000000.000100",
    }
    d = demux_message_event(event, bot_user_id="U_BOT")
    assert d is not None
    assert d.keys[0].format() == "thread:C012:1710000000.000100"


def test_demux_skips_message_changed():
    event = {
        "type": "message",
        "subtype": "message_changed",
        "channel": "C012",
        "ts": "1.2",
        "text": "x",
    }
    assert demux_message_event(event, bot_user_id="U_BOT") is None
