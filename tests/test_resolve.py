from pathlib import Path

import pytest

from slack_fd.cli import main
from slack_fd.errors import AmbiguousResolve, ResolveError
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.records import MessageRecord
from slack_fd.resolve import list_local_streams, resolve_name


class FakeResolveClient:
    def __init__(
        self,
        *,
        users: list[dict] | None = None,
        channels: list[dict] | None = None,
        open_map: dict[str, str] | None = None,
    ) -> None:
        self.users = users or []
        self.channels = channels or []
        self.open_map = open_map or {}

    def users_list(self, **kwargs):
        return {"ok": True, "members": self.users, "response_metadata": {}}

    def conversations_list(self, **kwargs):
        types = set((kwargs.get("types") or "").split(","))
        out = []
        for ch in self.channels:
            if ch.get("is_im") and "im" in types:
                out.append(ch)
            elif ch.get("is_mpim") and "mpim" in types:
                out.append(ch)
            elif ch.get("is_private") and "private_channel" in types:
                out.append(ch)
            elif (
                not ch.get("is_im")
                and not ch.get("is_mpim")
                and not ch.get("is_private")
                and "public_channel" in types
            ):
                out.append(ch)
        return {"ok": True, "channels": out, "response_metadata": {}}

    def conversations_open(self, **kwargs):
        users = kwargs.get("users")
        cid = self.open_map.get(users)
        if not cid:
            return {"ok": False, "error": "cannot_dm_bot"}
        return {"ok": True, "channel": {"id": cid}}


def test_from_dirname_roundtrip():
    k = StreamKey.thread("C012ABC", "1710000000.000100")
    assert StreamKey.from_dirname(k.encoded_dirname()) == k
    c = StreamKey.channel("D012XYZ")
    assert StreamKey.from_dirname(c.encoded_dirname()) == c


def test_resolve_passthrough_ids():
    r = resolve_name("D012ABCDE")
    assert r.key == "D012ABCDE"
    assert r.kind == "channel"
    r2 = resolve_name("C1:1.0")
    assert r2.key == "C1:1.0"
    assert r2.kind == "thread"


def test_resolve_user_and_channel():
    client = FakeResolveClient(
        users=[
            {
                "id": "U1",
                "name": "alice",
                "profile": {"display_name": "Alice"},
                "real_name": "Alice A",
            },
            {
                "id": "U2",
                "name": "bob",
                "profile": {"display_name": "Bob"},
            },
        ],
        channels=[
            {"id": "C9", "name": "general", "is_private": False},
            {"id": "C8", "name": "eng-private", "is_private": True},
        ],
        open_map={"U1": "D99"},
    )
    r = resolve_name("@alice", client=client)
    assert r.key == "D99"
    assert r.kind == "im"
    assert r.user_id == "U1"

    r = resolve_name("#general", client=client)
    assert r.key == "C9"
    assert r.name == "general"

    r = resolve_name("U1", client=client)
    assert r.key == "D99"

    with pytest.raises(ResolveError):
        resolve_name("@nobody", client=client)

    with pytest.raises(AmbiguousResolve):
        client2 = FakeResolveClient(
            users=[
                {"id": "U1", "name": "alice", "profile": {"display_name": "Alice"}},
                {"id": "U2", "name": "alina", "profile": {"display_name": "Alina"}},
            ],
            open_map={},
        )
        resolve_name("@ali", client=client2)


def test_list_local_streams(tmp_path: Path):
    store = LogStore(tmp_path)
    k = StreamKey.channel("D1")
    store.append(
        k,
        MessageRecord(offset=-1, direction="in", stream="", text="hi", ts="1.0"),
        log="inbox",
    )
    store.append(
        k,
        MessageRecord(
            offset=-1, direction="out", stream="", text="yo", status="pending"
        ),
        log="outbox",
    )
    # Legacy dir should be skipped
    (tmp_path / "streams" / "app__U1").mkdir(parents=True)

    rows = list_local_streams(tmp_path)
    assert len(rows) == 1
    assert rows[0].key == "D1"
    assert rows[0].inbox == 1
    assert rows[0].outbox == 1


def test_cli_ls_and_resolve(tmp_path: Path, monkeypatch, capsys):
    store = LogStore(tmp_path)
    store.append(
        StreamKey.channel("D1"),
        MessageRecord(offset=-1, direction="in", stream="", text="x", ts="1.0"),
        log="inbox",
    )
    try:
        main(["--data-dir", str(tmp_path), "ls"])
    except SystemExit as e:
        assert e.code == 0
    out = capsys.readouterr().out
    assert "D1" in out
    assert "inbox=1" in out

    client = FakeResolveClient(
        users=[{"id": "U1", "name": "alice", "profile": {"display_name": "Alice"}}],
        channels=[{"id": "C9", "name": "general"}],
        open_map={"U1": "D77"},
    )
    monkeypatch.setattr(
        "slack_fd.cli.resolve_name",
        lambda query, data_dir=None: resolve_name(query, client=client),
    )
    try:
        main(["--data-dir", str(tmp_path), "resolve", "@alice"])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == "D77"

    try:
        main(["--data-dir", str(tmp_path), "resolve", "D012ABC"])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == "D012ABC"
