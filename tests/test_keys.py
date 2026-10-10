import pytest

from slack_fd.errors import InvalidKey
from slack_fd.keys import StreamKey


def test_parse_thread():
    k = StreamKey.parse("thread:C01234567:1710000000.000100")
    assert k.kind == "thread"
    assert k.channel_id == "C01234567"
    assert k.thread_ts == "1710000000.000100"
    assert str(k) == "thread:C01234567:1710000000.000100"


def test_parse_dm_and_app():
    assert StreamKey.parse("dm:U012").user_id == "U012"
    assert StreamKey.parse("app:U012").kind == "app"


def test_constructors():
    assert StreamKey.thread("C1", "1.2").format() == "thread:C1:1.2"
    assert StreamKey.dm("U1").format() == "dm:U1"
    assert StreamKey.app("U1").format() == "app:U1"


def test_invalid():
    with pytest.raises(InvalidKey):
        StreamKey.parse("nope")
    with pytest.raises(InvalidKey):
        StreamKey.parse("thread:C1")
    with pytest.raises(InvalidKey):
        StreamKey.parse("app:")


def test_encoded_dirname():
    k = StreamKey.parse("thread:C1:1.2")
    assert ":" not in k.encoded_dirname()
    assert "." not in k.encoded_dirname()
