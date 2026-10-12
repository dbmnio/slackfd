import pytest

from slack_fd.errors import InvalidKey
from slack_fd.keys import StreamKey


def test_parse_channel():
    k = StreamKey.parse("C01234567")
    assert k.kind == "channel"
    assert k.channel_id == "C01234567"
    assert k.thread_ts is None
    assert str(k) == "C01234567"


def test_parse_im():
    k = StreamKey.parse("D01234567")
    assert k.kind == "channel"
    assert k.channel_id == "D01234567"


def test_parse_thread():
    k = StreamKey.parse("C01234567:1710000000.000100")
    assert k.kind == "thread"
    assert k.channel_id == "C01234567"
    assert k.thread_ts == "1710000000.000100"
    assert str(k) == "C01234567:1710000000.000100"


def test_constructors():
    assert StreamKey.channel("C1").format() == "C1"
    assert StreamKey.thread("C1", "1.2").format() == "C1:1.2"
    assert StreamKey.channel("D9").format() == "D9"


def test_invalid():
    with pytest.raises(InvalidKey):
        StreamKey.parse("nope")
    with pytest.raises(InvalidKey):
        StreamKey.parse("thread:C1:1.0")
    with pytest.raises(InvalidKey):
        StreamKey.parse("app:U1")
    with pytest.raises(InvalidKey):
        StreamKey.parse("U01234567")
    with pytest.raises(InvalidKey):
        StreamKey.parse("C1:")
    with pytest.raises(InvalidKey):
        StreamKey.parse("dm:U1")


def test_encoded_dirname():
    k = StreamKey.parse("C1:1.2")
    assert ":" not in k.encoded_dirname()
    assert "." not in k.encoded_dirname()
