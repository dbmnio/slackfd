import warnings

from slack_fd import SlackStream, StreamKey
from slack_fd.transport.embed import EmbedTransport


def test_embed_inject_read_without_slack_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    key = StreamKey.app("U1")
    transport = EmbedTransport(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with SlackStream(key, transport=transport, data_dir=tmp_path) as s:
            transport.inject_inbound(key, "hello")
            msg = s.read(timeout=1.0)
            assert msg is not None and msg.text == "hello"
            out = s.write("yo")
            assert out.status == "pending"  # no slack client
    assert any("embedding Socket Mode" in str(w.message) for w in caught)
