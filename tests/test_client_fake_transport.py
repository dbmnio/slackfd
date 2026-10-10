import threading
import time

from slack_fd import SlackStream, StreamKey
from slack_fd.transport.fake import FakeTransport


def test_write_and_inject_read(tmp_path):
    key = StreamKey.thread("C1", "10.0")
    transport = FakeTransport(tmp_path)
    with SlackStream(key, transport=transport) as s:
        out = s.write("hello")
        assert out.direction == "out"
        assert out.offset == 0

        transport.inject_inbound(key, "world", user="U1")
        msg = s.read(timeout=1.0)
        assert msg is not None
        assert msg.text == "world"
        assert msg.offset == 0

        # cursor advanced
        assert s.read(timeout=0.1) is None


def test_read_at_does_not_move_cursor(tmp_path):
    key = StreamKey.dm("U2")
    transport = FakeTransport(tmp_path)
    transport.inject_inbound(key, "one")
    transport.inject_inbound(key, "two")
    with SlackStream(key, transport=transport) as s:
        assert s.read_at(0).text == "one"
        assert s.read_at(1).text == "two"
        assert s.read(timeout=0.1).text == "one"
        assert s.read(timeout=0.1).text == "two"


def test_blocking_read_woken_by_inject(tmp_path):
    key = StreamKey.app("U3")
    transport = FakeTransport(tmp_path)
    result = []

    def reader():
        with SlackStream(key, transport=transport) as s:
            result.append(s.read(timeout=2.0))

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    transport.inject_inbound(key, "later")
    t.join(timeout=2.0)
    assert result and result[0] is not None
    assert result[0].text == "later"


def test_follow(tmp_path):
    key = StreamKey.dm("U4")
    transport = FakeTransport(tmp_path)
    with SlackStream(key, transport=transport) as s:
        transport.inject_inbound(key, "a")
        transport.inject_inbound(key, "b")
        it = s.follow()
        assert next(it).text == "a"
        assert next(it).text == "b"
