#!/usr/bin/env python3.12
"""PoC: Slack DM ↔ Cursor Agent (headless --print), via slack_fd.

Prereqs:
  - SLACK_BOT_TOKEN / SLACK_APP_TOKEN set; ``sfd daemon start``
  - Cursor Agent at ~/.local/bin/agent (not the Grok ``agent`` on PATH)
  - Logged in: ``~/.local/bin/agent login`` / status
  - Python >= 3.11 (shebang uses 3.12)

Example:
  export SFD_STREAM='D01234567'   # Slack IM channel id
  # from repo root, with deps available:
  .venv/bin/python examples/slack_cursor_bridge.py --workspace "$PWD"
  # or: python3.12 examples/slack_cursor_bridge.py --workspace "$PWD"

Stop with Ctrl-C.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# Allow running from a repo checkout without install.
_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from slack_fd import SlackStream  # noqa: E402

# Prefer the Cursor binary explicitly — ~/.grok/bin/agent often shadows PATH.
_DEFAULT_AGENT = Path.home() / ".local" / "bin" / "agent"

_SLACK_SOFT_LIMIT = 3500  # stay under typical Slack message comfort zone

_SYSTEM_HINT = textwrap.dedent(
    """\
    You are answering via Slack DM. Keep replies short and scannable:
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


def _run_agent(
    agent: Path,
    prompt: str,
    *,
    workspace: Path,
    force: bool,
    sandbox: str,
    continue_session: bool,
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
    ]
    if force:
        cmd.append("--force")
    if continue_session:
        cmd.append("--continue")
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
        return f"_agent error (exit {proc.returncode}):_\n```\n{_truncate(detail, 1500)}\n```"
    if not out:
        return f"_agent returned empty stdout._\n```\n{_truncate(err, 1500)}\n```"
    return _truncate(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--stream",
        default=os.environ.get("SFD_STREAM"),
        help="Stream key, e.g. D… or C…:ts (or env SFD_STREAM)",
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
    p.add_argument(
        "--no-continue",
        action="store_true",
        help="Do not pass --continue (each Slack msg is a fresh session)",
    )
    p.add_argument("--model", default=os.environ.get("SFD_AGENT_MODEL"))
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds to wait for each Slack read (default: block forever)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Handle a single inbound message then exit",
    )
    args = p.parse_args()

    if not args.stream:
        _log("bridge: pass --stream D…/C…[:ts] or set SFD_STREAM")
        return 2

    agent = _resolve_agent(args.agent)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        _log(f"bridge: workspace is not a directory: {workspace}")
        return 2

    force = not args.no_force
    use_continue = not args.no_continue
    started_session = False

    _log(f"bridge: stream={args.stream!r} workspace={workspace}")
    _log(f"bridge: agent={agent}")
    _log(f"bridge: force={force} sandbox={args.sandbox} continue={use_continue}")
    _log(
        "bridge: opening SlackStream with transport=daemon "
        "(client IPC only — does not start Socket Mode)"
    )

    # Force daemon transport. ``auto`` can fall back to embed Socket Mode, which
    # fights the background daemon (Slack allows one Socket Mode connection).
    with SlackStream(args.stream, transport="daemon") as s:
        _log("bridge: connected to daemon over UDS. Waiting for inbox…")
        _log(
            "bridge: tip — ``lsof`` on daemon.sock often shows *two* lines while "
            "this client is connected (listen + accepted conn); that is normal. "
            "Two different *daemon PIDs* is the bad case."
        )
        _log("bridge: watch uplink: tail -f ~/.local/share/sfd/daemon.log")
        while True:
            _log("bridge: read() …")
            msg = s.read(timeout=args.timeout)
            if msg is None:
                _log("bridge: read timeout / empty")
                if args.once:
                    return 1
                continue

            text = (msg.text or "").strip()
            if not text:
                _log("bridge: skipped empty message")
                continue

            _log(f"bridge: in ← {text[:120]!r}")
            _log("bridge: invoking cursor agent (this can take a while)…")

            prompt = f"{_SYSTEM_HINT}\n\n---\nSlack message:\n{text}"
            reply = _run_agent(
                agent,
                prompt,
                workspace=workspace,
                force=force,
                sandbox=args.sandbox,
                continue_session=use_continue and started_session,
                model=args.model,
            )
            started_session = True

            _log(f"bridge: agent done ({len(reply)} chars); writing to Slack…")
            out = s.write(reply)
            _log(
                f"bridge: out → status={out.status} offset={out.offset} chars={len(reply)}"
            )

            if args.once:
                return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _log("\nbridge: stopped")
        raise SystemExit(130) from None
