# slack_fd (`sfd`)

A file-descriptor-like interface to Slack conversations: open a stream by key, `read` / `write` messages, and look back through durable append-only logs. Backed by a lightweight daemon that owns the Socket Mode websocket.

This is **not** a Slack SDK replacement. The Slack Bolt/SDK remains the way to speak Slack’s protocol. This project only adds: demux/mux by conversation key, durable logs + cursors, a pull-based stream API, and a CLI that fits unix pipes.

---

## Goals

- **FD-like API** — open a logical stream, block on `read`, `write` outbound messages, replay history by offset.
- **No silent drops** — incoming and outgoing messages are appended to logs; consumers advance cursors.
- **Shared uplink** — one Socket Mode connection (daemon) shared by many CLI invocations and library clients.
- **Thin surface** — stream open/read/write/follow/ack only; no channels API, views, OAuth helpers, etc.
- **Straightforward UX** — hide reconnect, framing, and Socket Mode ack details behind the daemon.

## Non-goals

- Replacing or wrapping the full Slack Web API / Bolt app model.
- Exactly-once delivery across processes (at-least-once + idempotent consumers is enough).
- Multi-workspace complexity in v1 (single Slack app / workspace config).
- Being a general message bus for non-Slack transports.

---

## Why this exists (value add)

Bolt/SDK already handle websocket connect/reconnect, event dispatch, and `chat.postMessage`. They do **not** provide:

1. Pull-based per-conversation I/O (`read()` / `write()` on a thread or DM)
2. Multi-process sharing of one Socket Mode connection
3. Append-only history + cursors when no app code was running
4. Shell as a first-class client (`echo hi | sfd …`)

The durable stream + daemon **is** the product. The library is a thin client over that model, not a second SDK.

---

## Architecture decisions

### AD-1 — Language: Python

| Option | Verdict |
|--------|---------|
| **Python** | **Chosen.** Official Bolt Socket Mode SDK; natural sync + async library APIs; good fit for scripts/agents/CLI. |
| JavaScript/TS | Strong official SDK, but sync API is awkward; weaker unix-pipe aesthetic. |
| Java | Official SDK; too heavy for a local FD/CLI tool (JVM startup, distribution). |
| Go | Excellent daemon/CLI binary story; no official SDK — viable later if distribution becomes the bottleneck. |

Socket Mode protocol work in Go is feasible later; v1 prioritizes official SDK + dual sync/async API.

### AD-2 — Transport: Slack Socket Mode

- Inbound events: Socket Mode websocket (daemon owns the connection).
- Outbound messages: Slack Web API (`chat.postMessage`, etc.) — Socket Mode is not a bidirectional message pipe.
- The stream abstraction still presents one logical FD; two transports underneath are an implementation detail.

### AD-3 — Process model: daemon default, embed fallback

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ sfd CLI     │  │ lib client  │  │ lib client  │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       └────────────┬───┴────────────────┘
                    │ Unix domain socket (RPC)
                    ▼
            ┌───────────────┐
            │  sfd-daemon   │  ← owns Socket Mode WS + Web API sends
            └───────────────┘
                    │
                    ▼
                 Slack
```

- **Default:** clients connect to the daemon.
- **Fallback:** if the daemon is missing, the library may **auto-embed** a connection manager in-process and **emit a warning**.
- Embed mode is for solo use only. Slack effectively allows one active Socket Mode connection per app; two embeds will conflict. Warning text should direct users to `sfd daemon start` for shared use.

### AD-4 — Persistence: append-only JSONL + cursors (kafka-lite)

Not Kafka. Per-stream logs on disk, inspectable with `tail` / `jq`.

```
~/.local/share/sfd/
  config.toml
  daemon.pid
  daemon.sock
  streams/
    <stream_key_encoded>/
      inbox.jsonl      # received messages
      outbox.jsonl     # intended sends + status
      meta.json        # key, created_at, slack ids
      cursors/
        <consumer_id>  # last acked inbox offset
```

- **Inbox:** every relevant inbound message appended once (daemon demux).
- **Outbox:** every `write` appended before/with send; status updated (`pending` → `sent` | `failed`).
- **Cursors:** per consumer id; `read()` returns next record after cursor and advances on ack (see API).
- **Retention:** configurable max bytes/age later; v1 keeps everything local until explicitly trimmed.

SQLite is a possible later upgrade for indexing; not required for v1.

### AD-5 — Delivery semantics: at-least-once

- Messages are not dropped from logs when clients disconnect.
- `read` may redeliver if a client crashes before ack.
- Consumers should be prepared for duplicates (compare Slack `ts` / client message ids).

### AD-6 — Library scope: thin stream API only

Supported concepts:

- Stream keys (thread / DM / app DM)
- `open` / `read` / `write` / `read_at` / `follow` / ack / cursor
- Daemon lifecycle helpers (`ensure_daemon`, connect)

Not in library:

- Full chat/conversations/users surface
- Block Kit builders, modals, slash-command frameworks
- OAuth install flows (config expects tokens already provisioned)

### AD-7 — CLI is synchronous

Shell pipes are a blocking I/O model. The CLI uses the sync client only. Async belongs in the library for in-process async apps, not in `sfd` itself.

### AD-8 — IPC: Unix domain socket + length-prefixed JSON RPC

Simple, debuggable, enough for local trusted clients. No protobuf requirement for v1. Platform note: v1 targets macOS/Linux; Windows named pipes later if needed.

Socket path: prefer `<data_dir>/daemon.sock` when short enough; otherwise a hashed path under `$TMPDIR` (macOS `AF_UNIX` path length limit). The chosen path is recorded in `<data_dir>/daemon.sockpath`.

---

## Stream keys (day 1)

Streams are addressed by a small sum type. Wire/CLI form is a single string.

| Kind | Key form | Meaning |
|------|----------|---------|
| Thread | `thread:<channel_id>:<thread_ts>` | Channel or group DM thread (parent `thread_ts`) |
| User DM | `dm:<user_id>` | IM with a user (resolved to channel internally) |
| App / bot DM | `app:` | Messages directed at the bot’s DM conversation with the installing user(s); see note below |

Examples:

```text
thread:C01234567:1710000000.000100
dm:U01234567
app:
```

### Demux rules (inbound)

| Slack event | Stream key |
|-------------|------------|
| `message` in channel/MPIM with `thread_ts` (or replies) | `thread:<channel>:<thread_ts>` |
| `message` in channel with no thread (top-level) | Optionally `thread:<channel>:<ts>` treating the message as its own thread root — **v1: yes**, so top-level channel messages are addressable |
| `message` in IM with a user | `dm:<other_user_id>` |
| `message` in IM with the bot (user→bot) | `app:` (and also visible under `dm:<user_id>` if we can attribute — **v1: write to `app:` primary; mirror metadata includes `user`**) |

Exact bot-DM routing should use Slack’s IM channel id + bot user id from auth.test, stored in daemon meta at startup.

### Mux rules (outbound)

| Stream key | Send behavior |
|------------|---------------|
| `thread:…` | `chat.postMessage` to channel with `thread_ts` |
| `dm:U…` | Open/lookup IM for user, then `chat.postMessage` |
| `app:` | `chat.postMessage` to the app’s DM channel for the target user (CLI may require `--user` if ambiguous; library takes explicit user in write options for multi-user workspaces) |

**v1 simplification for `app:` writes:** require `user_id` in write options when more than one user talks to the bot; reads on `app:` interleave all user→bot DMs with `user` on each record (or we split to `app:<user_id>`).

**Revised day-1 choice (clearer):** use `app:<user_id>` for bot DMs per user, and reserve bare `app:` only as a fan-in follow that is CLI sugar over all `app:*` (optional, later). **Day 1 implement `app:<user_id>` only.**

Final day-1 key set:

```text
thread:<channel_id>:<thread_ts>
dm:<user_id>
app:<user_id>
```

- `dm:` — you messaging a user (bot as participant in IM).
- `app:` — user messaging the bot (same IM channel in practice; key distinguishes intent/cursor namespaces if needed, or aliases to same log).

**Implementation note:** For a typical bot IM, `dm:U` and `app:U` may map to the **same Slack channel**. Prefer **one physical log** keyed by canonical IM channel id, with **aliases** `dm:U` / `app:U` pointing at it, so read cursors can still be separate per alias if useful. V1 may use one cursor namespace per alias.

---

## Record shape

JSONL lines are JSON objects (one message per line). Offsets are 0-based line numbers (or monotonic ids if we add a header field — v1: line offset).

```json
{
  "offset": 0,
  "ts": "1710000000.000200",
  "direction": "in",
  "stream": "thread:C012:1710000000.000100",
  "channel": "C01234567",
  "user": "U01234567",
  "thread_ts": "1710000000.000100",
  "text": "hello",
  "raw": { }
}
```

- `raw` — truncated/optional Slack payload for debugging; full payload may be stored or referenced.
- Outbox adds `status`, `client_msg_id`, `error`.

---

## Library API (thin)

### Sync

```python
from slack_fd import SlackStream, StreamKey

key = StreamKey.parse("thread:C012:1710000000.000100")
# or StreamKey.thread(channel, thread_ts), .dm(user), .app(user)

with SlackStream(key) as s:
    msg = s.read()              # block until next unread for default consumer
    msg = s.read(timeout=30)    # None / raise on timeout — pick one; prefer None
    s.write("hello")
    s.write("hello", user_id="U…")  # when required
    past = s.read_at(0)         # absolute offset; does not move cursor
    for msg in s.follow():      # block and yield forever
        ...
```

### Async

```python
from slack_fd import AsyncSlackStream

async with AsyncSlackStream(key) as s:
    msg = await s.read()
    await s.write("hello")
    async for msg in s.follow():
        ...
```

### Connection behavior

```python
SlackStream(key)                     # daemon, else embed + warning
SlackStream(key, transport="daemon") # fail if daemon down
SlackStream(key, transport="embed")  # in-process; warn if daemon also running
```

### Consumer / cursor

- Default consumer id: `default` (CLI), or process-stable id if set via env/`consumer=`.
- `read()` → return next message after cursor, then ack (advance). Optional `read(ack=False)` for peek + explicit `ack(offset)`.

---

## CLI

```bash
# Daemon
sfd daemon start
sfd daemon stop
sfd daemon status

# Write (stdin → stream)
echo "hello" | sfd write thread:C012:1710000000.000100
echo "hello" | sfd dm:U01234567          # sugar: write if stdin is a pipe
echo "hello" | sfd app:U01234567

# Read next (stdout)
sfd read thread:C012:1710000000.000100
sfd thread:C012:1710000000.000100        # default verb: read if tty/stdout focus;
                                         # write if stdin is pipe — see UX note

# History / follow
sfd read --from 0 thread:…
sfd follow thread:…
sfd log thread:…                         # dump inbox/outbox with offsets
```

**UX note:** Convenient `echo hi | sfd <key>` and `sfd <key>` for read are desirable. Implement via stdin-isatty detection **or** explicit `read`/`write` subcommands (explicit preferred for scripts; sugar allowed for interactive use). Document both; prefer explicit subcommands in examples to avoid footguns.

---

## Module layout

```text
slack_fd/
  ARCHITECTURE.md          # this file
  README.md                # quick start (later)
  pyproject.toml
  src/
    slack_fd/
      __init__.py          # public: SlackStream, AsyncSlackStream, StreamKey
      keys.py              # StreamKey parse/format
      records.py           # Message record types
      logstore.py          # JSONL inbox/outbox + cursors
      transport/
        __init__.py
        base.py            # Transport protocol
        daemon.py          # UDS RPC client
        embed.py           # in-process connection manager
      client_sync.py       # SlackStream
      client_async.py      # AsyncSlackStream
      daemon/
        __init__.py
        server.py          # UDS server loop
        slack_conn.py      # Bolt Socket Mode + Web API
        demux.py           # event → stream key → logstore
        mux.py             # outbox drain → chat.postMessage
      cli.py               # sfd entrypoint
  tests/
    test_keys.py
    test_logstore.py
    test_demux.py
    test_client_fake_transport.py
```

Entry points (pyproject):

- `sfd = slack_fd.cli:main`

---

## Daemon responsibilities

1. Load config/tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, or config file).
2. Connect Socket Mode; auto-reconnect (Bolt).
3. On event: ack promptly → demux → append inbox JSONL → notify waiters over UDS.
4. On client write RPC: append outbox → send via Web API → update status.
5. Serve read/follow/log/cursor RPCs from logstore.
6. PID file + socket path; graceful shutdown.

---

## Robustness principles (vs OS sockets)

| Painful socket behavior | sfd behavior |
|-------------------------|--------------|
| Partial reads / framing | Message-oriented records only |
| EAGAIN / non-blocking soup | Blocking `read` with optional timeout |
| Connection state exposed | Daemon hides reconnect |
| Data lost if process down | JSONL + cursors |
| Errno archaeology | Small typed error set (`DaemonUnavailable`, `Timeout`, `SendFailed`, `InvalidKey`) |

---

## Config

Environment and/or `~/.local/share/sfd/config.toml`:

```toml
bot_token = "xoxb-..."
app_token = "xapp-..."
# optional
socket_path = "~/.local/share/sfd/daemon.sock"
data_dir = "~/.local/share/sfd"
```

Never commit tokens. Prefer env vars in production/agent settings.

---

## Security (local)

- UDS socket mode `0600`, directory `0700`.
- Trusted local user model only; no auth protocol on the socket in v1.
- Logs may contain message text — treat data dir as sensitive.

---

## Implementation phases

### Phase 0 — Skeleton ✅

- Package layout, `StreamKey`, logstore, fake transport, sync client against fake.
- CLI `read`/`write`/`inject`/`log`/`follow` against fake/logstore only (no Slack).

### Phase 1 — Daemon + IPC ✅

- UDS length-prefixed JSON RPC, daemon start/stop/status.
- Client → daemon path; auto falls back to fake + warning until Phase 2 embed.
- Slack app setup docs: [SLACK_SETUP.md](SLACK_SETUP.md).

### Phase 2 — Slack Socket Mode ✅

- Bolt Socket Mode in the daemon; demux for `thread` / `dm` / `app`; outbox send via Web API.
- Auto still falls back to fake + warning if daemon down (embed deferred).
- End-to-end: real Slack ↔ `sfd read`/`write` when tokens are configured.

### Phase 3 — Async + polish (in progress)

- ✅ `AsyncSlackStream` via `asyncio.to_thread` over the sync client.
- ✅ Merged chat *view* (`history()` / `sfd log --chat`) — physical inbox/outbox stay separate.
- ✅ Chat table formatting for `--chat`.

### Task backlog (implementation order)

Queues stay local-first. Slack is not the source of truth for cursors; sync is catch-up / repair only.

| Order | Task | Why this order |
|------:|------|----------------|
| 1 | **`sfd sync <key>`** — pull `conversations.history` / `replies`; append missing inbound by `ts`; repair outbox `pending`/`failed` (match bot messages → `sent` + `ts`) | Fixes live divergence now; unblocks trustworthy `--chat` |
| 2 | **Embed transport** — in-process Socket Mode when daemon missing (with warning) | Completes AD-3; depends on stable Slack bridge used by sync/daemon |
| 3 | **Retention / trim** — max age/bytes for JSONL | Safety polish after sync can grow logs |
| 4 | CLI pipe sugar / UX nits | Lowest risk; after core paths solid |

Non-goals for sync: continuous bidirectional mirror, rewriting inbox history, moving read cursors automatically.

---

## Open questions (deferred, not blocking)

1. Should top-level channel messages auto-create `thread:<channel>:<ts>` streams? (**lean yes**)
2. Separate cursors for inbox vs unified timeline of in+out? (**lean: inbox cursor for `read`; `log` shows both**)
3. Message subtype filtering (bot_message, message_changed, deletes)? (**v1: store text messages; skip noisy subtypes or store with flags**)
4. Distribution: pip-only vs eventual Go rewrite for single binary.

---

## Summary

| Decision | Choice |
|----------|--------|
| Language | Python + official Bolt |
| Inbound | Socket Mode |
| Outbound | Web API |
| Process | Daemon default; embed + warning fallback |
| Storage | Per-stream JSONL + cursors |
| Keys (day 1) | `thread:…`, `dm:…`, `app:<user_id>` |
| Library | Thin stream API (sync + async) |
| CLI | Sync only; explicit read/write + pipe sugar |
| Not doing | Full Slack SDK layer |
