"""Append-only JSONL inbox/outbox + per-consumer cursors."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterator, Literal

from slack_fd.keys import StreamKey
from slack_fd.paths import streams_dir
from slack_fd.records import MessageRecord, OutboxStatus

LogName = Literal["inbox", "outbox"]


class LogStore:
    """Per-stream durable logs under the data directory."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._root = streams_dir(data_dir)
        self._lock = threading.RLock()

    def stream_dir(self, key: StreamKey) -> Path:
        path = self._root / key.encoded_dirname()
        path.mkdir(parents=True, exist_ok=True)
        (path / "cursors").mkdir(exist_ok=True)
        meta = path / "meta.json"
        if not meta.exists():
            meta.write_text(
                json.dumps({"stream": key.format(), "kind": key.kind}, indent=2) + "\n",
                encoding="utf-8",
            )
        return path

    def _log_path(self, key: StreamKey, name: LogName) -> Path:
        return self.stream_dir(key) / f"{name}.jsonl"

    def append(
        self,
        key: StreamKey,
        record: MessageRecord,
        *,
        log: LogName = "inbox",
    ) -> MessageRecord:
        """Append a record; assigns offset as next line index."""
        with self._lock:
            path = self._log_path(key, log)
            offset = self._line_count(path)
            record.offset = offset
            record.stream = key.format()
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            return record

    def read_at(
        self,
        key: StreamKey,
        offset: int,
        *,
        log: LogName = "inbox",
    ) -> MessageRecord | None:
        with self._lock:
            path = self._log_path(key, log)
            if not path.exists() or offset < 0:
                return None
            with path.open(encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i == offset:
                        return MessageRecord.from_dict(json.loads(line))
            return None

    def iter_from(
        self,
        key: StreamKey,
        start_offset: int = 0,
        *,
        log: LogName = "inbox",
    ) -> Iterator[MessageRecord]:
        with self._lock:
            path = self._log_path(key, log)
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for i, line in enumerate(lines):
            if i < start_offset:
                continue
            if not line.strip():
                continue
            yield MessageRecord.from_dict(json.loads(line))

    def next_after_cursor(
        self,
        key: StreamKey,
        consumer_id: str,
        *,
        log: LogName = "inbox",
    ) -> MessageRecord | None:
        """Return the next unread record without advancing the cursor."""
        cursor = self.get_cursor(key, consumer_id)
        return self.read_at(key, cursor + 1, log=log)

    def at_inbox_tip(self, key: StreamKey, consumer_id: str) -> bool:
        """True when the consumer has no unread inbox messages locally."""
        return self.next_after_cursor(key, consumer_id, log="inbox") is None

    def inbox_len(self, key: StreamKey) -> int:
        return self._line_count(self._log_path(key, "inbox"))

    def record_at_inbox_cursor(
        self,
        key: StreamKey,
        consumer_id: str,
    ) -> MessageRecord | None:
        """Message at the consumer's last-acked inbox offset, or None if unread-none."""
        cursor = self.get_cursor(key, consumer_id)
        if cursor < 0:
            return None
        return self.read_at(key, cursor, log="inbox")

    def last_sent_outbox(self, key: StreamKey) -> MessageRecord | None:
        """Newest outbox row with status=sent and a Slack ``ts``, or None."""
        last: MessageRecord | None = None
        for rec in self.iter_from(key, 0, log="outbox"):
            if rec.status == "sent" and rec.ts:
                last = rec
        return last

    def get_cursor(self, key: StreamKey, consumer_id: str) -> int:
        """Last acked offset; -1 means nothing acked yet."""
        path = self._cursor_path(key, consumer_id)
        if not path.exists():
            return -1
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return -1
        return int(text)

    def set_cursor(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        path = self._cursor_path(key, consumer_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(offset) + "\n", encoding="utf-8")

    def ack(self, key: StreamKey, consumer_id: str, offset: int) -> None:
        with self._lock:
            current = self.get_cursor(key, consumer_id)
            if offset > current:
                self.set_cursor(key, consumer_id, offset)

    def update_outbox_status(
        self,
        key: StreamKey,
        offset: int,
        status: OutboxStatus,
        *,
        ts: str | None = None,
        error: str | None = None,
    ) -> MessageRecord | None:
        """Rewrite outbox.jsonl updating one line (small local logs)."""
        with self._lock:
            path = self._log_path(key, "outbox")
            if not path.exists():
                return None
            lines = path.read_text(encoding="utf-8").splitlines()
            if offset < 0 or offset >= len(lines):
                return None
            rec = MessageRecord.from_dict(json.loads(lines[offset]))
            rec.status = status
            if ts is not None:
                rec.ts = ts
            if error is not None:
                rec.error = error
            if status == "sent":
                rec.error = None
            lines[offset] = json.dumps(rec.to_dict(), ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return rec

    def iter_chat(self, key: StreamKey) -> list[tuple[LogName, MessageRecord]]:
        """Merged inbox+outbox view in conversation order (Slack ts, then log/offset).

        Physical logs stay separate; this is a read-only projection for display.
        """
        rows: list[tuple[LogName, MessageRecord]] = []
        for name in ("inbox", "outbox"):
            for rec in self.iter_from(key, 0, log=name):  # type: ignore[arg-type]
                rows.append((name, rec))
        rows.sort(key=lambda item: _chat_sort_key(item[0], item[1]))
        return rows

    def trim(
        self,
        key: StreamKey,
        *,
        inbox_keep: int | None = None,
        outbox_keep: int | None = None,
    ) -> dict[str, int]:
        """Drop oldest lines, keeping the newest N per log. Adjusts inbox cursors."""
        with self._lock:
            dropped_in = (
                self._trim_log(key, "inbox", inbox_keep) if inbox_keep is not None else 0
            )
            dropped_out = (
                self._trim_log(key, "outbox", outbox_keep)
                if outbox_keep is not None
                else 0
            )
            return {
                "inbox_dropped": dropped_in,
                "outbox_dropped": dropped_out,
            }

    def _trim_log(self, key: StreamKey, name: LogName, keep: int) -> int:
        if keep < 0:
            raise ValueError("keep must be >= 0")
        path = self._log_path(key, name)
        if not path.exists():
            return 0
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) <= keep:
            return 0
        dropped = len(lines) - keep
        kept = lines[-keep:] if keep else []
        rewritten: list[str] = []
        for i, line in enumerate(kept):
            rec = MessageRecord.from_dict(json.loads(line))
            rec.offset = i
            rewritten.append(json.dumps(rec.to_dict(), ensure_ascii=False))
        path.write_text(
            ("\n".join(rewritten) + "\n") if rewritten else "",
            encoding="utf-8",
        )
        if name == "inbox" and dropped:
            cursors = self.stream_dir(key) / "cursors"
            if cursors.exists():
                for cursor_path in cursors.iterdir():
                    if not cursor_path.is_file():
                        continue
                    try:
                        cur = int(cursor_path.read_text(encoding="utf-8").strip() or "-1")
                    except ValueError:
                        cur = -1
                    cursor_path.write_text(str(max(-1, cur - dropped)) + "\n", encoding="utf-8")
        return dropped

    def _cursor_path(self, key: StreamKey, consumer_id: str) -> Path:
        safe = consumer_id.replace("/", "_").replace("..", "_")
        return self.stream_dir(key) / "cursors" / safe

    @staticmethod
    def _line_count(path: Path) -> int:
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as f:
            return sum(1 for _ in f)


def _chat_sort_key(log_name: LogName, rec: MessageRecord) -> tuple:
    """Sort by Slack ts when present; otherwise after timestamped rows."""
    ts = rec.ts
    if ts:
        raw = ts.removeprefix("fake-") if ts.startswith("fake-") else ts
        try:
            return (0, float(raw), 0 if log_name == "inbox" else 1, rec.offset)
        except ValueError:
            pass
    return (1, 0.0, 0 if log_name == "inbox" else 1, rec.offset)
