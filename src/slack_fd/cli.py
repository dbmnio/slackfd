"""sfd command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from slack_fd.client_sync import SlackStream
from slack_fd.daemon import lifecycle
from slack_fd.errors import DaemonUnavailable, InvalidKey, SlackFdError
from slack_fd.keys import StreamKey
from slack_fd.logstore import LogStore
from slack_fd.paths import default_data_dir
from slack_fd.transport.daemon import DaemonTransport
from slack_fd.transport.fake import FakeTransport


def _data_dir(args: argparse.Namespace) -> Path | None:
    if getattr(args, "data_dir", None):
        return Path(args.data_dir).expanduser()
    return None


def _resolve_transport(args: argparse.Namespace) -> Any:
    """Prefer daemon; fall back to embed (or fake if forced)."""
    from slack_fd.transport.embed import EmbedTransport

    data = _data_dir(args)
    mode = os.environ.get("SFD_TRANSPORT", "auto")
    if mode == "fake":
        return FakeTransport(data)
    if mode == "embed":
        return EmbedTransport(data)
    if mode == "daemon":
        t = DaemonTransport(data)
        t.connect()
        return t
    try:
        t = DaemonTransport(data)
        t.connect()
        return t
    except DaemonUnavailable:
        return EmbedTransport(data)


def cmd_write(args: argparse.Namespace) -> int:
    key = StreamKey.parse(args.key)
    text = args.message
    if text is None:
        text = sys.stdin.read()
        if text.endswith("\n") and text.count("\n") == 1:
            text = text[:-1]
    if text == "" and not args.allow_empty:
        print("sfd: empty message (pass --allow-empty to send)", file=sys.stderr)
        return 2

    transport = _resolve_transport(args)
    try:
        with SlackStream(
            key, transport=transport, consumer_id=args.consumer, data_dir=_data_dir(args)
        ) as s:
            rec = s.write(text, user_id=args.user)
    finally:
        transport.close()
    if args.json:
        print(json.dumps(rec.to_dict(), ensure_ascii=False))
    else:
        print(f"wrote outbox offset={rec.offset} stream={rec.stream}", file=sys.stderr)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    key = StreamKey.parse(args.key)
    transport = _resolve_transport(args)
    try:
        with SlackStream(
            key, transport=transport, consumer_id=args.consumer, data_dir=_data_dir(args)
        ) as s:
            if args.from_offset is not None:
                rec = s.read_at(args.from_offset)
                if rec is not None and args.ack:
                    s.ack(rec.offset)
            else:
                rec = s.read(timeout=args.timeout, ack=not args.no_ack)
    finally:
        transport.close()

    if rec is None:
        return 1
    if args.json:
        print(json.dumps(rec.to_dict(), ensure_ascii=False))
    else:
        print(rec.text)
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    key = StreamKey.parse(args.key)
    transport = _resolve_transport(args)
    try:
        with SlackStream(
            key, transport=transport, consumer_id=args.consumer, data_dir=_data_dir(args)
        ) as s:
            try:
                for rec in s.follow(ack=not args.no_ack):
                    if args.json:
                        print(json.dumps(rec.to_dict(), ensure_ascii=False), flush=True)
                    else:
                        print(rec.text, flush=True)
            except KeyboardInterrupt:
                return 0
    finally:
        transport.close()
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    key = StreamKey.parse(args.key)
    store = LogStore(_data_dir(args) or default_data_dir())
    if args.chat:
        rows = store.iter_chat(key)
        if args.json:
            for name, rec in rows:
                row = rec.to_dict()
                row["_log"] = name
                print(json.dumps(row, ensure_ascii=False))
            return 0
        _print_chat_table(rows)
        return 0

    which = args.which
    logs = ("inbox", "outbox") if which == "both" else (which,)
    for name in logs:
        for rec in store.iter_from(key, args.from_offset, log=name):  # type: ignore[arg-type]
            if args.json:
                row = rec.to_dict()
                row["_log"] = name
                print(json.dumps(row, ensure_ascii=False))
            else:
                print(f"{name}\t{rec.offset}\t{rec.direction}\t{rec.text}")
    return 0


def _print_chat_table(rows: list) -> None:
    """Aligned chat projection; physical logs remain separate."""
    if not rows:
        return
    header = ("dir", "ref", "ts", "status", "text")
    table: list[tuple[str, str, str, str, str]] = [header]
    for name, rec in rows:
        table.append(
            (
                rec.direction,
                f"{name}:{rec.offset}",
                rec.ts or "—",
                rec.status or "—",
                rec.text.replace("\t", " ").replace("\n", "\\n"),
            )
        )
    widths = [max(len(row[i]) for row in table) for i in range(4)]
    for i, row in enumerate(table):
        line = (
            f"{row[0]:<{widths[0]}}  "
            f"{row[1]:<{widths[1]}}  "
            f"{row[2]:<{widths[2]}}  "
            f"{row[3]:<{widths[3]}}  "
            f"{row[4]}"
        )
        print(line)
        if i == 0:
            print(
                f"{'─' * widths[0]}  "
                f"{'─' * widths[1]}  "
                f"{'─' * widths[2]}  "
                f"{'─' * widths[3]}  "
                f"{'─' * max(4, min(40, max(len(r[4]) for r in table)))}"
            )


def cmd_inject(args: argparse.Namespace) -> int:
    """Inject a fake inbound message (no Slack); wakes daemon waiters if connected."""
    key = StreamKey.parse(args.key)
    text = args.message if args.message is not None else sys.stdin.read().rstrip("\n")
    transport = _resolve_transport(args)
    try:
        transport.open(key)
        rec = transport.inject_inbound(key, text, user=args.user)
    finally:
        transport.close()
    if args.json:
        print(json.dumps(rec.to_dict(), ensure_ascii=False))
    else:
        print(f"injected inbox offset={rec.offset} stream={rec.stream}", file=sys.stderr)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Slack → local: catch up inbox; repair outbox already in Slack (no send)."""
    from slack_fd.fetch import fetch_stream

    key = StreamKey.parse(args.key)
    data = _data_dir(args) or default_data_dir()
    result: dict

    try:
        transport = DaemonTransport(data)
        transport.connect()
        try:
            result = transport.fetch(key, limit=args.limit)
        finally:
            transport.close()
    except (DaemonUnavailable, SlackFdError):
        result = fetch_stream(key, data_dir=data, limit=args.limit).to_dict()

    verb = getattr(args, "cmd", None) or "fetch"
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"{verb} {result.get('key')} channel={result.get('channel')} "
            f"fetched={result.get('fetched')} "
            f"inbound_added={result.get('inbound_added')} "
            f"outbox_repaired={result.get('outbox_repaired')} "
            f"skipped={result.get('skipped')}"
        )
    return 0


def cmd_flush(args: argparse.Namespace) -> int:
    """Send pending/failed outbox to Slack (repair-from-history first by default)."""
    from slack_fd.flush import flush_stream

    key = StreamKey.parse(args.key)
    data = _data_dir(args) or default_data_dir()
    result: dict

    try:
        transport = DaemonTransport(data)
        transport.connect()
        try:
            result = transport.flush(
                key,
                retry_failed=not args.no_retry_failed,
                repair_first=not args.no_repair,
                limit=args.limit,
            )
        finally:
            transport.close()
    except (DaemonUnavailable, SlackFdError):
        result = flush_stream(
            key,
            data_dir=data,
            retry_failed=not args.no_retry_failed,
            repair_first=not args.no_repair,
            limit=args.limit,
        ).to_dict()

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"flush {result.get('key')} "
            f"repaired={result.get('repaired')} "
            f"sent={result.get('sent')} "
            f"failed={result.get('failed')} "
            f"skipped={result.get('skipped')}"
        )
        for note in result.get("notes") or []:
            print(f"  note: {note}", file=sys.stderr)
    return 0 if not result.get("failed") else 1


def cmd_trim(args: argparse.Namespace) -> int:
    key = StreamKey.parse(args.key)
    inbox_keep = args.inbox_keep if args.inbox_keep is not None else args.keep
    outbox_keep = args.outbox_keep if args.outbox_keep is not None else args.keep
    if inbox_keep is None and outbox_keep is None:
        print("sfd: pass --keep N and/or --inbox-keep / --outbox-keep", file=sys.stderr)
        return 2
    store = LogStore(_data_dir(args) or default_data_dir())
    result = store.trim(key, inbox_keep=inbox_keep, outbox_keep=outbox_keep)
    if args.json:
        print(json.dumps(result))
    else:
        print(
            f"trim {key} inbox_dropped={result['inbox_dropped']} "
            f"outbox_dropped={result['outbox_dropped']}"
        )
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    data = _data_dir(args)
    action = args.action or "status"
    if action == "start":
        info = lifecycle.start(data)
        print(json.dumps(info, indent=2) if args.json else _fmt_daemon(info))
        return 0
    if action == "stop":
        info = lifecycle.stop(data)
        print(json.dumps(info, indent=2) if args.json else _fmt_daemon(info))
        return 0
    info = lifecycle.status(data)
    print(json.dumps(info, indent=2) if args.json else _fmt_daemon(info))
    return 0 if info.get("running") else 1


def _fmt_daemon(info: dict) -> str:
    if "started" in info:
        if info.get("already_running"):
            return f"daemon already running pid={info.get('pid')} sock={info.get('socket')}"
        return f"daemon started pid={info.get('pid')} sock={info.get('socket')}"
    if "stopped" in info:
        if not info.get("was_running"):
            return "daemon was not running"
        return f"daemon stopped pid={info.get('pid')}"
    running = info.get("running")
    return (
        f"running={running} pid={info.get('pid')} "
        f"sock={info.get('socket')} data_dir={info.get('data_dir')}"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sfd",
        description="File-descriptor-like Slack streams (daemon + local logs)",
    )
    p.add_argument(
        "--data-dir",
        help="Override data directory (default: $SFD_DATA_DIR or ~/.local/share/sfd)",
    )
    p.add_argument(
        "--consumer",
        default="default",
        help="Cursor consumer id (default: default)",
    )
    p.add_argument("--json", action="store_true", help="JSON output")

    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="Append/send a message to a stream")
    w.add_argument("key", help="Stream key (thread:… / dm:… / app:…)")
    w.add_argument("message", nargs="?", help="Message text (default: stdin)")
    w.add_argument("--user", help="User id when required for app/dm writes")
    w.add_argument("--allow-empty", action="store_true")
    w.set_defaults(func=cmd_write)

    r = sub.add_parser("read", help="Read next inbox message (or --from offset)")
    r.add_argument("key")
    r.add_argument("--timeout", type=float, default=None, help="Seconds; default: block")
    r.add_argument("--from", dest="from_offset", type=int, default=None)
    r.add_argument("--no-ack", action="store_true", help="Do not advance cursor")
    r.add_argument(
        "--ack",
        action="store_true",
        help="With --from, advance cursor to that offset",
    )
    r.set_defaults(func=cmd_read)

    f = sub.add_parser("follow", help="Follow inbox like tail -f")
    f.add_argument("key")
    f.add_argument("--no-ack", action="store_true")
    f.set_defaults(func=cmd_follow)

    lg = sub.add_parser("log", help="Dump inbox/outbox with offsets")
    lg.add_argument("key")
    lg.add_argument(
        "--which",
        choices=("inbox", "outbox", "both"),
        default="both",
    )
    lg.add_argument("--from", dest="from_offset", type=int, default=0)
    lg.add_argument(
        "--chat",
        action="store_true",
        help="Merged conversation view (inbox+outbox by Slack ts); logs stay separate",
    )
    lg.set_defaults(func=cmd_log)

    inj = sub.add_parser(
        "inject",
        help="Inject fake inbound message (testing / Phase 1; no Slack)",
    )
    inj.add_argument("key")
    inj.add_argument("message", nargs="?")
    inj.add_argument("--user", default="U_FAKE")
    inj.set_defaults(func=cmd_inject)

    fe = sub.add_parser(
        "fetch",
        help="Slack → local: catch up inbox; repair outbox already in Slack (no send)",
    )
    fe.add_argument("key")
    fe.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max history messages to fetch (default 200)",
    )
    fe.set_defaults(func=cmd_fetch)

    # Deprecated alias — "sync" sounded bidirectional.
    sy = sub.add_parser(
        "sync",
        help="Deprecated alias for fetch (Slack → local only)",
    )
    sy.add_argument("key")
    sy.add_argument("--limit", type=int, default=200)
    sy.set_defaults(func=cmd_fetch)

    fl = sub.add_parser(
        "flush",
        help="Local → Slack: send pending/failed outbox (repairs from history first)",
    )
    fl.add_argument("key")
    fl.add_argument(
        "--limit",
        type=int,
        default=200,
        help="History window for pre-flush repair (default 200)",
    )
    fl.add_argument(
        "--no-repair",
        action="store_true",
        help="Do not match existing Slack bot messages before sending (risk of dupes)",
    )
    fl.add_argument(
        "--no-retry-failed",
        action="store_true",
        help="Only send status=pending rows; leave failed alone",
    )
    fl.set_defaults(func=cmd_flush)

    tr = sub.add_parser("trim", help="Drop oldest log lines; keep newest N")
    tr.add_argument("key")
    tr.add_argument(
        "--keep",
        type=int,
        default=None,
        help="Keep newest N lines in both inbox and outbox",
    )
    tr.add_argument("--inbox-keep", type=int, default=None)
    tr.add_argument("--outbox-keep", type=int, default=None)
    tr.set_defaults(func=cmd_trim)

    d = sub.add_parser("daemon", help="Manage background daemon")
    d.add_argument("action", choices=("start", "stop", "status"), nargs="?", default="status")
    d.set_defaults(func=cmd_daemon)

    return p


_SUBCOMMANDS = frozenset(
    {
        "write",
        "read",
        "follow",
        "log",
        "inject",
        "fetch",
        "sync",
        "flush",
        "trim",
        "daemon",
    }
)
_OPTS_WITH_VALUE = frozenset({"--data-dir", "--consumer"})


def apply_key_sugar(argv: list[str], *, stdin_is_tty: bool | None = None) -> list[str]:
    """Rewrite ``sfd <key>`` into read/write based on whether stdin is a pipe.

    - ``sfd app:U1`` → ``sfd read app:U1`` (tty stdin)
    - ``echo hi | sfd app:U1`` → ``sfd write app:U1`` (piped stdin)
    Explicit subcommands are left unchanged.
    """
    if stdin_is_tty is None:
        stdin_is_tty = sys.stdin.isatty()

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _OPTS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("--data-dir=") or tok.startswith("--consumer="):
            i += 1
            continue
        if tok in {"--json", "-h", "--help"}:
            i += 1
            continue
        if tok.startswith("-"):
            # Unknown global flag; don't invent sugar ahead of it.
            i += 1
            continue
        if tok in _SUBCOMMANDS:
            return argv
        try:
            StreamKey.parse(tok)
        except InvalidKey:
            return argv
        verb = "read" if stdin_is_tty else "write"
        return [*argv[:i], verb, *argv[i:]]
    return argv


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    argv = apply_key_sugar(raw)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = args.func(args)
    except InvalidKey as e:
        print(f"sfd: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    except DaemonUnavailable as e:
        print(f"sfd: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    except SlackFdError as e:
        print(f"sfd: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    raise SystemExit(code)


if __name__ == "__main__":
    main()
