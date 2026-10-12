import threading
import time

import pytest

from slack_fd import SlackStream, StreamKey
from slack_fd.daemon import lifecycle
from slack_fd.daemon.server import DaemonServer
from slack_fd.errors import DaemonUnavailable
from slack_fd.transport.daemon import DaemonTransport


@pytest.fixture
def daemon_server(tmp_path):
    server = DaemonServer(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if server.sock_path.exists():
            break
        time.sleep(0.01)
    else:
        server.stop()
        raise RuntimeError("daemon server failed to bind")
    yield server
    server.stop()
    thread.join(timeout=2)


def test_daemon_rpc_write_inject_read(daemon_server, tmp_path):
    key = StreamKey.thread("C1", "1.0")
    t = DaemonTransport(tmp_path)
    t.open(key)
    out = t.write(key, "hello")
    assert out.direction == "out"
    assert out.status == "pending"

    inj = t.inject_inbound(key, "world", user="U1")
    assert inj.text == "world"

    with SlackStream(key, transport=t) as s:
        msg = s.read(timeout=1.0)
        assert msg is not None
        assert msg.text == "world"
    t.close()


def test_daemon_blocking_read(daemon_server, tmp_path):
    key = StreamKey.channel("D9")
    result = []

    def reader():
        t = DaemonTransport(tmp_path)
        with SlackStream(key, transport=t) as s:
            result.append(s.read(timeout=2.0))
        t.close()

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.1)
    inj = DaemonTransport(tmp_path)
    inj.inject_inbound(key, "ping")
    inj.close()
    thread.join(timeout=3)
    assert result and result[0] is not None
    assert result[0].text == "ping"


def test_lifecycle_start_stop(tmp_path):
    info = lifecycle.start(tmp_path)
    assert info["started"] or info.get("already_running")
    assert lifecycle.is_running(tmp_path)

    st = lifecycle.status(tmp_path)
    assert st["running"] is True
    assert st["daemon"]["phase"] == 1

    key = "D1"
    t = DaemonTransport(tmp_path)
    t.inject_inbound(StreamKey.parse(key), "via-subprocess-daemon")
    with SlackStream(key, transport=t) as s:
        assert s.read(timeout=1.0).text == "via-subprocess-daemon"
    t.close()

    stopped = lifecycle.stop(tmp_path)
    assert stopped["stopped"] is True
    assert not lifecycle.is_running(tmp_path)


def test_daemon_transport_unavailable(tmp_path):
    t = DaemonTransport(tmp_path)
    with pytest.raises(DaemonUnavailable):
        t.connect()
