import threading
import time

import pytest

from slack_fd import SlackStream, StreamKey
from slack_fd.daemon.server import DaemonServer
from slack_fd.errors import DaemonUnavailable
from slack_fd.ipc import read_daemon_token, read_tcp_endpoint
from slack_fd.transport.daemon import DaemonTransport


@pytest.fixture
def tcp_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("SFD_IPC", "tcp")
    monkeypatch.setenv("SFD_TCP_HOST", "127.0.0.1")
    monkeypatch.setenv("SFD_TCP_PORT", "0")  # ephemeral; written to daemon.tcp

    server = DaemonServer(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if read_tcp_endpoint(tmp_path):
            break
        time.sleep(0.01)
    else:
        server.stop()
        raise RuntimeError("tcp daemon failed to bind")
    yield server
    server.stop()
    thread.join(timeout=2)


def test_tcp_requires_token_and_roundtrips(tcp_daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("SFD_IPC", "tcp")
    assert read_daemon_token(tmp_path)
    key = StreamKey.app("U1")
    t = DaemonTransport(tmp_path)
    t.open(key)
    t.inject_inbound(key, "via-tcp")
    with SlackStream(key, transport=t) as s:
        assert s.read(timeout=1.0).text == "via-tcp"
    t.close()


def test_tcp_rejects_bad_token(tcp_daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("SFD_IPC", "tcp")
    token_path = tmp_path / "daemon.token"
    token_path.write_text("wrong-token\n", encoding="utf-8")
    t = DaemonTransport(tmp_path)
    with pytest.raises(DaemonUnavailable, match="invalid|missing|unauthorized"):
        t.connect()
