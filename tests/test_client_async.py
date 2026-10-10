import asyncio

from slack_fd import AsyncSlackStream, StreamKey
from slack_fd.transport.fake import FakeTransport


def test_async_read_write(tmp_path):
    key = StreamKey.dm("U7")
    transport = FakeTransport(tmp_path)

    async def run() -> None:
        async with AsyncSlackStream(key, transport=transport, data_dir=tmp_path) as s:
            await s.write("out")
            transport.inject_inbound(key, "in")
            msg = await s.read(timeout=1.0)
            assert msg is not None
            assert msg.text == "in"
            hist = await s.history()
            texts = [r.text for _, r in hist]
            assert "out" in texts and "in" in texts

    asyncio.run(run())
