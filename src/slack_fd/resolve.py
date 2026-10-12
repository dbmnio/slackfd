"""Resolve human names to Slack channel ids; list local / remote conversations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from slack_fd.config import load_config
from slack_fd.daemon.mux import response_data
from slack_fd.errors import AmbiguousResolve, InvalidKey, ResolveError
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore

_USER_ID_RE = re.compile(r"^U[A-Z0-9]+$")


class ResolveWebApi(Protocol):
    def users_list(self, **kwargs: Any) -> Any: ...

    def conversations_list(self, **kwargs: Any) -> Any: ...

    def conversations_open(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class Resolved:
    """A stream key plus optional Slack labels for humans."""

    key: str
    kind: str  # channel | private_channel | im | mpim | thread
    name: str | None = None
    user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LocalStreamInfo:
    key: str
    kind: str
    inbox: int
    outbox: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _UserHit:
    user_id: str
    name: str
    score: int


@dataclass
class _ChanHit:
    channel_id: str
    name: str
    kind: str
    score: int


def resolve_name(
    query: str,
    *,
    data_dir: Path | None = None,
    client: ResolveWebApi | None = None,
) -> Resolved:
    """Map ``@user``, ``#channel``, ``U…``, or ``C|D|G…`` to a stream channel id.

    Users resolve to their IM channel (``D…``) via ``conversations.open``.
    Already-valid channel / thread keys pass through.
    """
    raw = (query or "").strip()
    if not raw:
        raise ResolveError("empty resolve query")

    try:
        sk = StreamKey.parse(raw)
        return Resolved(key=sk.format(), kind=sk.kind, name=None)
    except InvalidKey:
        pass

    stripped = raw.lstrip("#@").strip()
    if not stripped:
        raise ResolveError(f"empty resolve query after stripping {raw!r}")

    if _USER_ID_RE.fullmatch(stripped):
        api = client or _default_client(data_dir)
        return _open_im(api, stripped)

    api = client or _default_client(data_dir)

    if raw.startswith("#"):
        return _resolve_channel_name(api, stripped)
    if raw.startswith("@"):
        return _resolve_user_name(api, stripped)

    # Bare token: prefer exact channel name, then user.
    try:
        return _resolve_channel_name(api, stripped)
    except ResolveError:
        return _resolve_user_name(api, stripped)


def list_local_streams(data_dir: Path | None = None) -> list[LocalStreamInfo]:
    store = LogStore(data_dir)
    out: list[LocalStreamInfo] = []
    for key in store.list_streams():
        out.append(
            LocalStreamInfo(
                key=key.format(),
                kind=key.kind,
                inbox=store.inbox_len(key),
                outbox=store.outbox_len(key),
            )
        )
    return out


def list_remote_conversations(
    *,
    data_dir: Path | None = None,
    client: ResolveWebApi | None = None,
    types: str = "public_channel,private_channel,im,mpim",
) -> list[Resolved]:
    """Conversations visible to the bot (Slack Web API)."""
    api = client or _default_client(data_dir)
    users_by_id = {u["id"]: u for u in _iter_users(api)}
    results: list[Resolved] = []
    for ch in _iter_conversations(api, types=types):
        cid = str(ch.get("id") or "")
        if not cid:
            continue
        if ch.get("is_im"):
            uid = str(ch.get("user") or "")
            uname = _user_label(users_by_id.get(uid) or {"id": uid})
            results.append(
                Resolved(key=cid, kind="im", name=uname, user_id=uid or None)
            )
        elif ch.get("is_mpim"):
            results.append(
                Resolved(key=cid, kind="mpim", name=str(ch.get("name") or cid))
            )
        elif ch.get("is_private"):
            results.append(
                Resolved(
                    key=cid,
                    kind="private_channel",
                    name=str(ch.get("name") or cid),
                )
            )
        else:
            results.append(
                Resolved(key=cid, kind="channel", name=str(ch.get("name") or cid))
            )
    results.sort(key=lambda r: (r.kind, (r.name or "").lower(), r.key))
    return results


def _resolve_channel_name(client: ResolveWebApi, name: str) -> Resolved:
    name_l = name.lower().lstrip("#")
    hits: list[_ChanHit] = []
    for ch in _iter_conversations(
        client, types="public_channel,private_channel,mpim"
    ):
        cid = str(ch.get("id") or "")
        cname = str(ch.get("name") or "")
        if not cid or not cname:
            continue
        kind = (
            "mpim"
            if ch.get("is_mpim")
            else "private_channel"
            if ch.get("is_private")
            else "channel"
        )
        cname_l = cname.lower()
        if cname_l == name_l:
            hits.append(_ChanHit(cid, cname, kind, score=0))
        elif cname_l.startswith(name_l) or name_l in cname_l:
            hits.append(_ChanHit(cid, cname, kind, score=1))
    if not hits:
        raise ResolveError(f"no channel matching {name!r}")
    hits.sort(key=lambda h: (h.score, h.name.lower(), h.channel_id))
    best_score = hits[0].score
    top = [h for h in hits if h.score == best_score]
    if len(top) > 1:
        raise AmbiguousResolve(
            f"multiple channels match {name!r}; be more specific",
            candidates=[f"{h.channel_id}\t#{h.name}" for h in top[:20]],
        )
    h = top[0]
    return Resolved(key=h.channel_id, kind=h.kind, name=h.name)


def _resolve_user_name(client: ResolveWebApi, name: str) -> Resolved:
    name_l = name.lower().lstrip("@")
    hits: list[_UserHit] = []
    for u in _iter_users(client):
        if u.get("deleted") or u.get("is_bot"):
            continue
        uid = str(u.get("id") or "")
        if not uid:
            continue
        uname = str(u.get("name") or "")
        profile = u.get("profile") or {}
        display = str(profile.get("display_name") or "")
        real = str(u.get("real_name") or profile.get("real_name") or "")
        labels_l = [x.lower() for x in (uname, display, real) if x]
        if name_l in labels_l:
            hits.append(_UserHit(uid, _user_label(u), score=0))
        elif any(x.startswith(name_l) or name_l in x for x in labels_l):
            hits.append(_UserHit(uid, _user_label(u), score=1))
    if not hits:
        raise ResolveError(f"no user matching {name!r}")
    hits.sort(key=lambda h: (h.score, h.name.lower(), h.user_id))
    best_score = hits[0].score
    top = [h for h in hits if h.score == best_score]
    if len(top) > 1:
        raise AmbiguousResolve(
            f"multiple users match {name!r}; be more specific",
            candidates=[f"{h.user_id}\t@{h.name}" for h in top[:20]],
        )
    return _open_im(client, top[0].user_id, label=top[0].name)


def _open_im(
    client: ResolveWebApi, user_id: str, *, label: str | None = None
) -> Resolved:
    data = response_data(client.conversations_open(users=user_id))
    if not data.get("ok"):
        raise ResolveError(
            f"conversations.open failed for {user_id}: {data.get('error')}"
        )
    ch = data.get("channel") or {}
    cid = str(ch.get("id") or "")
    if not cid:
        raise ResolveError(f"conversations.open returned no channel for {user_id}")
    return Resolved(key=cid, kind="im", name=label, user_id=user_id)


def _iter_users(client: ResolveWebApi) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        data = response_data(client.users_list(**kwargs))
        if not data.get("ok"):
            raise ResolveError(f"users.list failed: {data.get('error')}")
        out.extend(list(data.get("members") or []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return out


def _iter_conversations(
    client: ResolveWebApi, *, types: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "types": types,
            "exclude_archived": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        data = response_data(client.conversations_list(**kwargs))
        if not data.get("ok"):
            raise ResolveError(f"conversations.list failed: {data.get('error')}")
        out.extend(list(data.get("channels") or []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return out


def _user_label(user: dict[str, Any]) -> str:
    profile = user.get("profile") or {}
    display = str(profile.get("display_name") or "").strip()
    if display:
        return display
    name = str(user.get("name") or "").strip()
    if name:
        return name
    return str(user.get("id") or "")


def _default_client(data_dir: Path | None) -> ResolveWebApi:
    cfg = load_config(data_dir)
    if not cfg.bot_token:
        raise ResolveError(
            "SLACK_BOT_TOKEN required for resolve/ls --remote "
            "(see SLACK_SETUP.md)"
        )
    from slack_sdk import WebClient

    return WebClient(token=cfg.bot_token)
