#!/usr/bin/env python3.12
"""PoC: Slack channel ↔ Cursor Agent, one agent chat per Slack thread.

Listens on a channel stream. Top-level messages start a new Slack thread
(reply under that message) and a new Cursor chat (``agent create-chat``).
Later replies in that thread resume the same Cursor chat via ``--resume``.

Prereqs:
  - SLACK_BOT_TOKEN / SLACK_APP_TOKEN set
  - Bot invited to the channel; ``channels:history`` (or groups) + ``chat:write``
  - **Exactly one** ``sfd daemon start`` already running (this script uses
    ``transport=daemon`` only — it does not start Socket Mode)
  - Cursor Agent at ~/.local/bin/agent (not the Grok ``agent`` on PATH)
  - Logged in: ``~/.local/bin/agent login``

Example:
  sfd daemon start
  export SFD_CHANNEL='C01234567'
  .venv/bin/python examples/slack_cursor_bridge.py --workspace "$PWD"

Stop with Ctrl-C.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

# Allow running from a repo checkout without install.
_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from slack_fd import SlackStream, StreamKey  # noqa: E402

_DEFAULT_AGENT = Path.home() / ".local" / "bin" / "agent"
_SLACK_SOFT_LIMIT = 3500

_SYSTEM_HINT = textwrap.dedent(
    """\
    You are answering in a Slack channel thread. Keep replies short and scannable:
    - Prefer a few sentences or a tight bullet list
    - No tool narration, no long stack traces unless asked
    - Plain text / light markdown only (Slack mrkdwn)
    """
).strip()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _resolve_agent(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise SystemExit(f"agent not found: {p}")
        return p
    env = os.environ.get("CURSOR_AGENT")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
    if _DEFAULT_AGENT.is_file():
        return _DEFAULT_AGENT
    which = shutil.which("cursor-agent") or shutil.which("agent")
    if which:
        return Path(which)
    raise SystemExit(
        "Cursor agent not found. Expected ~/.local/bin/agent "
        "(or set --agent / CURSOR_AGENT)."
    )


def _truncate(text: str, limit: int = _SLACK_SOFT_LIMIT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n\n…(truncated)"


class ChatMap:
    """Persist Slack thread key → Cursor chat id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log(f"bridge: could not load chat map {self.path}: {e}")
            return
        if isinstance(raw, dict):
            self._data = {str(k): str(v) for k, v in raw.items() if k and v}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def get(self, thread_key: str) -> str | None:
        with self._lock:
            return self._data.get(thread_key)

    def set(self, thread_key: str, chat_id: str) -> None:
        with self._lock:
            self._data[thread_key] = chat_id
            self.save()

    def items(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._data.items())


def _create_chat(agent: Path) -> str:
    proc = subprocess.run(
        [str(agent), "create-chat"],
        capture_output=True,
        text=True,
        check=False,
    )
    chat_id = (proc.stdout or "").strip().splitlines()
    chat_id = chat_id[-1].strip() if chat_id else ""
    if proc.returncode != 0 or not chat_id:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(f"create-chat failed: {err}")
    return chat_id


def _run_agent(
    agent: Path,
    prompt: str,
    *,
    workspace: Path,
    force: bool,
    sandbox: str,
    chat_id: str,
    model: str | None,
) -> str:
    cmd: list[str] = [
        str(agent),
        "-p",
        "--output-format",
        "text",
        "--trust",
        "--workspace",
        str(workspace),
        "--sandbox",
        sandbox,
        "--resume",
        chat_id,
    ]
    if force:
        cmd.append("--force")
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err or out or f"exit {proc.returncode}"
        return (
            f"_agent error (exit {proc.returncode}):_\n"
            f"```\n{_truncate(detail, 1500)}\n```"
        )
    if not out:
        return f"_agent returned empty stdout._\n```\n{_truncate(err, 1500)}\n```"
    return _truncate(out)


def _consumer_for(stream_key: str) -> str:
    return "bridge-" + StreamKey.parse(stream_key).encoded_dirname()


def _reply_in_thread(
    thread_key: str,
    text: str,
    *,
    agent: Path,
    workspace: Path,
    force: bool,
    sandbox: str,
    chat_id: str,
    model: str | None,
) -> None:
    prompt = f"{_SYSTEM_HINT}\n\n---\nSlack message:\n{text}"
    _log(f"bridge: agent resume={chat_id} thread={thread_key}")
    reply = _run_agent(
        agent,
        prompt,
        workspace=workspace,
        force=force,
        sandbox=sandbox,
        chat_id=chat_id,
        model=model,
    )
    # Daemon enqueues + sends on write when Socket Mode is up (no client fetch/flush).
    with SlackStream(
        thread_key,
        transport="daemon",
        consumer_id=_consumer_for(thread_key) + "-write",
    ) as s:
        out = s.write(reply)
    _log(
        f"bridge: out → {thread_key} status={out.status} "
        f"offset={out.offset} chars={len(reply)}"
    )
    if out.status != "sent":
        _log(
            "bridge: WARNING outbox not sent "
            "(is `sfd daemon` up with Slack tokens?)"
        )


def _follow_thread(
    thread_key: str,
    chat_id: str,
    *,
    agent: Path,
    workspace: Path,
    force: bool,
    sandbox: str,
    model: str | None,
    stop: threading.Event,
    seen_ts: set[str],
    seen_lock: threading.Lock,
) -> None:
    _log(f"bridge: following thread {thread_key} → chat {chat_id}")
    try:
        # Live demux fills the inbox; autofetch races with demux and used to
        # append the same Slack ts twice. Keep catch-up explicit if needed.
        with SlackStream(
            thread_key,
            transport="daemon",
            consumer_id=_consumer_for(thread_key),
            autofetch=False,
        ) as s:
            root_ts = StreamKey.parse(thread_key).thread_ts
            while not stop.is_set():
                msg = s.read(timeout=1.0)
                if msg is None:
                    continue
                # conversations.replies / fetch puts the parent on the thread
                # inbox; the channel loop already answered that opener.
                if root_ts and msg.ts == root_ts:
                    _log(
                        f"bridge: skip thread root ts={msg.ts} "
                        f"(already handled as top-level)"
                    )
                    continue
                with seen_lock:
                    if msg.ts and msg.ts in seen_ts:
                        _log(
                            f"bridge: skip duplicate inbound ts={msg.ts} "
                            f"text={((msg.text or '')[:60])!r}"
                        )
                        continue
                    if msg.ts:
                        seen_ts.add(msg.ts)
                text = (msg.text or "").strip()
                if not text:
                    continue
                _log(
                    f"bridge: thread ← {thread_key} ts={msg.ts} {text[:120]!r}"
                )
                try:
                    _reply_in_thread(
                        thread_key,
                        text,
                        agent=agent,
                        workspace=workspace,
                        force=force,
                        sandbox=sandbox,
                        chat_id=chat_id,
                        model=model,
                    )
                except Exception as e:  # noqa: BLE001 — keep follower alive
                    _log(f"bridge: thread handler error: {e}")
    except Exception as e:  # noqa: BLE001
        _log(f"bridge: thread follower exited ({thread_key}): {e}")


class Bridge:
    def __init__(
        self,
        *,
        channel: str,
        agent: Path,
        workspace: Path,
        force: bool,
        sandbox: str,
        model: str | None,
        chats: ChatMap,
    ) -> None:
        self.channel = StreamKey.channel(channel).format()
        self.agent = agent
        self.workspace = workspace
        self.force = force
        self.sandbox = sandbox
        self.model = model
        self.chats = chats
        self.stop = threading.Event()
        self._workers: dict[str, threading.Thread] = {}
        self._workers_lock = threading.Lock()
        self._seen_ts: dict[str, set[str]] = {}
        self._seen_lock = threading.Lock()

    def ensure_worker(self, thread_key: str, chat_id: str) -> None:
        with self._workers_lock:
            existing = self._workers.get(thread_key)
            if existing is not None and existing.is_alive():
                return
            with self._seen_lock:
                seen = self._seen_ts.setdefault(thread_key, set())
                root_ts = StreamKey.parse(thread_key).thread_ts
                if root_ts:
                    seen.add(root_ts)
            t = threading.Thread(
                target=_follow_thread,
                kwargs={
                    "thread_key": thread_key,
                    "chat_id": chat_id,
                    "agent": self.agent,
                    "workspace": self.workspace,
                    "force": self.force,
                    "sandbox": self.sandbox,
                    "model": self.model,
                    "stop": self.stop,
                    "seen_ts": seen,
                    "seen_lock": self._seen_lock,
                },
                name=f"sfd-bridge-{thread_key}",
                daemon=True,
            )
            self._workers[thread_key] = t
            t.start()

    def resume_workers(self) -> None:
        for thread_key, chat_id in self.chats.items():
            try:
                StreamKey.parse(thread_key)
            except Exception:  # noqa: BLE001
                _log(f"bridge: skip bad chat-map key {thread_key!r}")
                continue
            self.ensure_worker(thread_key, chat_id)

    def handle_top_level(self, msg_text: str, thread_key: str) -> None:
        chat_id = self.chats.get(thread_key)
        if chat_id is None:
            chat_id = _create_chat(self.agent)
            self.chats.set(thread_key, chat_id)
            _log(f"bridge: new chat {chat_id} for thread {thread_key}")
        else:
            _log(f"bridge: reuse chat {chat_id} for thread {thread_key}")

        _reply_in_thread(
            thread_key,
            msg_text,
            agent=self.agent,
            workspace=self.workspace,
            force=self.force,
            sandbox=self.sandbox,
            chat_id=chat_id,
            model=self.model,
        )
        # Follow further replies in this Slack thread.
        self.ensure_worker(thread_key, chat_id)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--channel",
        default=os.environ.get("SFD_CHANNEL") or os.environ.get("SFD_STREAM"),
        help="Channel id C… (or env SFD_CHANNEL / SFD_STREAM)",
    )
    p.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("SFD_AGENT_WORKSPACE", os.getcwd())),
        help="Cursor --workspace / cwd (default: cwd or SFD_AGENT_WORKSPACE)",
    )
    p.add_argument(
        "--agent",
        default=None,
        help="Path to Cursor agent (default: ~/.local/bin/agent)",
    )
    p.add_argument(
        "--sandbox",
        choices=("enabled", "disabled"),
        default=os.environ.get("SFD_AGENT_SANDBOX", "enabled"),
        help="Cursor --sandbox (default: enabled)",
    )
    p.add_argument(
        "--no-force",
        action="store_true",
        help="Do not pass --force (will likely hang on approvals)",
    )
    p.add_argument("--model", default=os.environ.get("SFD_AGENT_MODEL"))
    p.add_argument(
        "--chats-file",
        type=Path,
        default=None,
        help="JSON map of thread key → Cursor chat id "
        "(default: $SFD_DATA_DIR/bridge-chats/<channel>.json)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds to wait for each channel read (default: block forever)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Handle a single top-level channel message then exit",
    )
    args = p.parse_args()

    if not args.channel:
        _log("bridge: pass --channel C… or set SFD_CHANNEL")
        return 2

    try:
        channel_key = StreamKey.parse(args.channel)
    except Exception as e:  # noqa: BLE001
        _log(f"bridge: invalid channel: {e}")
        return 2
    if channel_key.thread_ts:
        _log("bridge: pass a channel id (C…/D…/G…), not a thread key")
        return 2

    agent = _resolve_agent(args.agent)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        _log(f"bridge: workspace is not a directory: {workspace}")
        return 2

    if args.chats_file:
        chats_path = args.chats_file.expanduser().resolve()
    else:
        from slack_fd.paths import default_data_dir

        chats_path = (
            default_data_dir()
            / "bridge-chats"
            / f"{channel_key.format()}.json"
        )
    chats = ChatMap(chats_path)
    bridge = Bridge(
        channel=channel_key.format(),
        agent=agent,
        workspace=workspace,
        force=not args.no_force,
        sandbox=args.sandbox,
        model=args.model,
        chats=chats,
    )

    _log(f"bridge: channel={bridge.channel!r} workspace={workspace}")
    _log(f"bridge: agent={agent}")
    _log(f"bridge: chats-file={chats_path} ({len(chats.items())} mapped)")
    _log("bridge: transport=daemon (does not start Socket Mode)")

    bridge.resume_workers()

    with SlackStream(
        bridge.channel,
        transport="daemon",
        consumer_id=_consumer_for(bridge.channel),
    ) as s:
        _log("bridge: listening for top-level channel messages…")
        while True:
            msg = s.read(timeout=args.timeout)
            if msg is None:
                _log("bridge: channel read timeout / empty")
                if args.once:
                    return 1
                continue

            text = (msg.text or "").strip()
            if not text:
                _log("bridge: skipped empty top-level message")
                continue

            thread_key = msg.reply_stream_key()
            if not thread_key or ":" not in thread_key:
                _log(f"bridge: could not derive thread key from msg ts={msg.ts!r}")
                continue

            _log(f"bridge: channel ← {text[:120]!r} → thread {thread_key}")
            try:
                bridge.handle_top_level(text, thread_key)
            except Exception as e:  # noqa: BLE001 — keep listening
                _log(f"bridge: top-level handler error: {e}")

            if args.once:
                bridge.stop.set()
                return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _log("\nbridge: stopped")
        raise SystemExit(130) from None
