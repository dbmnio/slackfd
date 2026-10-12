# slack_fd (`sfd`)

A file-descriptor-like interface to Slack conversations: open a stream by key, `read` / `write` messages, and look back through durable append-only logs. Backed by a lightweight daemon that owns the Socket Mode websocket.

This is **not** a Slack SDK replacement. The Slack Bolt/SDK remains the way to speak Slack’s protocol. This project only adds: demux/mux by conversation key, durable logs + cursors, a pull-based stream API, and a CLI that fits unix pipes.

---

## Goals

- **FD-like API** — open a logical stream, block on `read`, `write` outbound messages, replay history by offset.
- **No silent drops** — incoming and outgoing messages are appended to logs; consumers advance cursors.
- **Shared uplink** — one Socket Mode connection (daemon) shared by many CLI invocations and library clients.
- **Thin surface** — stream `read` / `write` (simple) plus explicit `fetch` / `flush` (advanced); no channels API, views, OAuth helpers, etc.
- **Straightforward UX** — hide reconnect, framing, and Socket Mode ack details behind the daemon.

## Non-goals

- Replacing or wrapping the full Slack Web API / Bolt app model.
- Exactly-once delivery across processes (at-least-once + idempotent consumers is enough).
- Multi-workspace complexity in v1 (single Slack app / workspace config).
- Being a general message bus for non-Slack transports.
- SQS-style explicit per-message ack as the primary API (local inbox cursor advances on `read`).
- Observable / push-to-user APIs (FD surface stays pull-shaped).

---

## Why this exists (value add)

Bolt/SDK already handle websocket connect/reconnect, event dispatch, and `chat.postMessage`. They do **not** provide:

1. Pull-based per-conversation I/O (`read()` / `write()` on a thread or DM)
2. Multi-process sharing of one Socket Mode connection
3. Append-only history + cursors when no app code was running
4. Shell as a first-class client (`echo hi | sfd …`)

The durable stream + daemon **is** the product. The library is a thin client over that model, not a second SDK.

---

## Cursor model (inbox / outbox asymmetry)

`read` and `write` are **not** symmetric operations. Symmetry is in the **gap** between a local cursor and a remote (Slack) cursor on each side.

```text
INBOX (messages originate remotely)
  local_inbox_cursor ──────────────► remote_inbox_cursor
                     │ gap: unknown locally │
                     │ fetch() fills gap    │
                     │ read() consumes +    │
                     │   advances local     │

OUTBOX (messages originate locally)
  remote_outbox_cursor ────────────► local_outbox_cursor
                     │ gap: known locally   │
                     │ flush() closes gap   │
                     │ write() appends +    │
                     │   advances local     │
```

| Op | Advances | Meaning |
|----|----------|---------|
| `read` | local inbox cursor | Consume next known inbox message |
| `fetch` | remote inbox cursor (locally) | Pull Slack → local store for the inbox gap |
| `write` | local outbox cursor | Append pending outbound message |
| `flush` | remote outbox cursor | Send pending → Slack; Slack accept is the ack |

### Assumptions about prefixes (before the gap)

| Prefix | Assumption | Realistic drift |
|--------|------------|-----------------|
| Inbox ≤ local cursor | Already-read history is correct | Edits/deletes of old msgs (uncommon for bot DMs; more common in human channels). True reorder / holes behind the cursor are very rare. |
| Outbox ≤ remote cursor | Slack-acked sends are correct | Almost never wrong if remote only advances on Slack accept. Crash pain lives **in the gap** (sent remotely but still `pending` locally) → heal on `fetch`/`flush`. |

Slack is the authority for **what exists in a conversation**. Local logs are a cache plus cursors. Healing reconciles the **gaps**, not the whole history every time.

### Naming: `fetch`, not `sync`

`fetch` = Slack → local (inbound catch-up + optional outbox repair from history).  
It is **not** bidirectional. Outbound drain is `flush`.  
(`sfd sync` remains a deprecated CLI alias for `fetch`.)

---

## Simple vs advanced API

### Simple (default)

Library/CLI keep Slack-side cursors up to date for you:

```text
read()
  if local inbox cursor is at the front of known inbox:
      fetch()                         # close remote→local inbox gap
  return next message
  advance local inbox cursor

write(text)
  append to outbox                    # advance local outbox cursor
  flush() until outbox gap is closed  # catch remote up
```

### Advanced

`read` / `write` only move **local** cursors (and the local store).  
The caller is responsible for `fetch` / `flush` to move **remote** cursors.

| Surface | Simple | Advanced |
|---------|--------|----------|
| Inbox catch-up | auto `fetch` when reading at tip | explicit `fetch` |
| Outbox drain | auto `flush` after `write` | explicit `flush` |
| Cursor on read | advances on `read` | same (optional peek flags later) |

---

## Pull backends (internal; poll later)

The FD API is pull-shaped. Push exists only **inside** a websocket adapter (Socket Mode events → inbox).

```text
InboundSource.pull(since) → [Message]
  websocket: drain buffered events (+ history heal when needed)
  poll:      conversations.history / replies each call  (not implemented yet)
```

Keep a narrow internal port if useful; a formal interface can wait until a second backend exists. No poll implementation required for current phases.

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
- History/`fetch` uses Web API to heal gaps when the websocket was down or events were missed.

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
        <consumer_id>  # last consumed inbox offset (advanced on read)
```

- **Inbox:** every relevant inbound message appended once (daemon demux and/or `fetch`).
- **Outbox:** every `write` appended before/with send; status updated (`pending` → `sent` | `failed`).
- **Cursors:** per consumer id; `read()` returns next record after cursor and advances the local inbox cursor.
- **Retention:** configurable max bytes/age later; v1 keeps everything local until explicitly trimmed (`sfd trim`).

SQLite is a possible later upgrade for indexing; not required for v1.

### AD-5 — Delivery semantics: at-least-once

- Messages are not dropped from logs when clients disconnect.
- `read` advances the local inbox cursor when the message is returned; a crash after deliver but before persisting the cursor may redeliver.
- Consumers should be prepared for duplicates (compare Slack `ts` / client message ids).
- Outbox duplicates after crash-in-gap are mitigated by heal-before-send (`fetch` / pre-`flush` history match).

### AD-6 — Library scope: thin stream API only

Supported concepts:

- Stream keys (thread / DM / app DM)
- Simple: `read` / `write` (auto `fetch` / `flush` as above)
- Advanced: explicit `fetch` / `flush`; `read_at` / `follow` / `log` / `chat` view
- Daemon lifecycle helpers (`ensure_daemon`, connect)

Not in library:

- Full chat/conversations/users surface
- Block Kit builders, modals, slash-command frameworks
- OAuth install flows (config expects tokens already provisioned)

### AD-7 — CLI is synchronous

Shell pipes are a blocking I/O model. The CLI uses the sync client only. Async belongs in the library for in-process async apps, not in `sfd` itself.

### AD-8 — IPC: Unix domain socket (default) or loopback TCP + length-prefixed JSON RPC

Simple, debuggable, enough for local trusted clients. No protobuf requirement for v1. Platform note: v1 targets macOS/Linux; Windows named pipes later if needed.

- **unix** (default): prefer `<data_dir>/daemon.sock` when short enough; otherwise a hashed path under `$TMPDIR` (macOS `AF_UNIX` path length limit). Path recorded in `<data_dir>/daemon.sockpath`. Auth = filesystem modes (`0600` / `0700`).
- **tcp** (`SFD_IPC=tcp`): bind **loopback only** (`127.0.0.1` / `::1`; non-loopback rejected). Auth token in `<data_dir>/daemon.token` (`0600`), sent on every RPC; endpoint in `<data_dir>/daemon.tcp`. For environments that block Unix domain sockets.

### AD-9 — Seek is inbound-only

Cursor seek / `read --from` applies to the **inbox** consumer cursor, not a unified chat timeline and not outbox. Full conversation inspection is `log --chat` (merged view; does not move the read cursor).

---

## Stream keys

Streams use Slack’s own ids. Wire/CLI form is a single string:

| Key form | Meaning |
|----------|---------|
| `<channel_id>` | Channel / IM / MPIM top-level tape (`C…` / `D…` / `G…`) |
| `<channel_id>:<thread_ts>` | Thread under that conversation (parent message `ts`) |

Examples:

```text
C01234567
D01234567
C01234567:1710000000.000100
```

### Demux rules (inbound)

| Slack event | Stream key |
|-------------|------------|
| Top-level message (no distinct `thread_ts`) | `<channel>` |
| Reply (`thread_ts` set and ≠ `ts`) | `<channel>:<thread_ts>` |

### Mux rules (outbound)

| Stream key | Send behavior |
|------------|---------------|
| `<channel>` | `chat.postMessage(channel=…)` |
| `<channel>:<thread_ts>` | `chat.postMessage(channel=…, thread_ts=…)` |

Discover channel ids via Slack UI / API (`conversations.list`, `conversations.open`); `sfd` does not invent user-shaped aliases.

---

## Record shape

JSONL lines are JSON objects (one message per line). Offsets are 0-based line numbers (or monotonic ids if we add a header field — v1: line offset).

```json
{
  "offset": 0,
  "ts": "1710000000.000200",
  "direction": "in",
  "stream": "C012:1710000000.000100",
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

### Sync (simple)

```python
from slack_fd import SlackStream, StreamKey

key = StreamKey.parse("C012:1710000000.000100")
# or StreamKey.channel("C012"), StreamKey.thread("C012", "1710000000.000100")

with SlackStream(key) as s:
    msg = s.read()              # fetch if at tip; return next; advance local inbox cursor
    msg = s.read(timeout=30)    # None on timeout
    s.write("hello")            # outbox + flush until sent
    past = s.read_at(0)         # absolute offset; does not move cursor
    for msg in s.follow():      # block and yield forever
        ...
```

### Advanced (explicit gap management)

```python
with SlackStream(key, autosync=False, autoflush=False) as s:  # names TBD
    s.fetch()                   # Slack → local (inbox gap + outbox heal)
    msg = s.read(timeout=0)     # local only; None if nothing new
    s.write("hello")            # pending only
    s.flush()                   # drain outbox
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
- Simple path: `read()` returns the next inbox message after the cursor and advances the local inbox cursor.
- Optional peek / `--no-ack` style flags may remain for debugging; they are not the primary model.

---

## CLI

```bash
# Daemon
sfd daemon start
sfd daemon stop
sfd daemon status

# Write (stdin → stream) — simple: enqueue + flush
echo "hello" | sfd write C012:1710000000.000100
echo "hello" | sfd D01234567             # sugar: write if stdin is a pipe

# Read next (stdout) — simple: fetch if at tip, then consume
sfd read C012:1710000000.000100
sfd D01234567                            # sugar: read if tty

# Advanced: manage gaps yourself
sfd fetch D012…                          # Slack → local
sfd flush D012…                          # local → Slack
sfd sync D012…                           # deprecated alias for fetch
sfd key -i D012…                         # reply/follow key from last read

# History / follow / views
sfd read --from 0 C012…                  # seek inbox cursor (inbound only)
sfd follow C012…
sfd log C012…                            # dump inbox/outbox with offsets
sfd log --chat C012…                     # merged view by Slack ts; does not move read cursor
sfd trim C012… --keep 1000
```


**UX note:** Convenient `echo hi | sfd <key>` and `sfd <key>` for read are desirable. Explicit `read`/`write` preferred in scripts; sugar allowed for interactive use.

| Command | Direction | FD-ish analogy | Role |
|---------|-----------|----------------|------|
| `sfd fetch` | Slack → local | Fill the receive buffer from the uplink | Catch up inbox; heal outbox already in Slack |
| `sfd flush` | Local → Slack | Drain the send buffer | Send remaining pending/failed outbox |
| `sfd sync` | — | — | Deprecated alias for `fetch` |

---

## Module layout

```text
slack_fd/
  ARCHITECTURE.md
  README.md
  SLACK_SETUP.md
  pyproject.toml
  src/
    slack_fd/
      __init__.py          # public: SlackStream, AsyncSlackStream, StreamKey
      keys.py
      records.py
      logstore.py          # JSONL inbox/outbox + cursors
      fetch.py             # Slack → local (inbox + outbox heal)
      flush.py             # local → Slack (outbox drain)
      transport/
        base.py
        daemon.py
        embed.py
        fake.py
      client_sync.py
      client_async.py
      daemon/
        server.py
        slack_conn.py
        service.py
      cli.py
  tests/
```

Entry points (pyproject):

- `sfd = slack_fd.cli:main`

---

## Daemon responsibilities

1. Load config/tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, or config file).
2. Connect Socket Mode; auto-reconnect (Bolt).
3. On event: ack promptly → demux → append inbox JSONL → notify waiters over UDS.
4. On client write RPC: append outbox → send via Web API → update status (or defer send to `flush` in advanced mode).
5. Serve read/follow/log/fetch/flush/cursor RPCs from logstore + Web API.
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
- Trusted local user model only; no auth protocol on the unix socket in v1.
- TCP mode requires a local auth token file (`0600`) and loopback bind only.
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
- Auto falls back to embed + warning if daemon down.
- End-to-end: real Slack ↔ `sfd read`/`write` when tokens are configured.

### Phase 3 — Async + polish ✅

- ✅ `AsyncSlackStream` via `asyncio.to_thread` over the sync client.
- ✅ Merged chat *view* (`history()` / `sfd log --chat`) — physical inbox/outbox stay separate.
- ✅ Chat table formatting for `--chat`.
- ✅ Fetch/flush, embed, trim, CLI key/pipe sugar, optional TCP IPC.

### Phase 4 — Simple API semantics ✅

Wire the cursor/gap model into default `read` / `write` behavior:

| Order | Task | Notes |
|------:|------|-------|
| 1 | ✅ Auto-`fetch` on `read` when at inbox tip | Soft-fail if Slack unavailable; does not rewrite prefix |
| 2 | ✅ Auto-`flush` on `write` until outbox gap closed | `write` appends pending; flush heals then sends |
| 3 | ✅ Opt-out flags / advanced mode | `autofetch=` / `autoflush=`; CLI `--no-fetch` / `--no-flush` |
| 4 | ✅ Seek/read documented as inbound-only | `log --chat` for full transcript |
| 5 | (Optional) internal `InboundSource` port | Websocket now; poll later if needed |

Non-goals for fetch: continuous bidirectional mirror, rewriting inbox history behind the local cursor, moving read cursors automatically during `fetch`.

---

## Open questions (deferred, not blocking)

1. Message subtype filtering (bot_message, message_changed, deletes)? (**v1: store text messages; skip noisy subtypes or store with flags**)
2. Distribution: pip-only vs eventual Go rewrite for single binary.
3. Name → channel_id resolve helpers (`sfd ls` / `resolve`).
4. Listen/accept for thread discovery under a channel supervisor.

---

## Summary

| Decision | Choice |
|----------|--------|
| Language | Python + official Bolt |
| Inbound | Socket Mode (+ history heal via `fetch`) |
| Outbound | Web API (`flush`) |
| Process | Daemon default; embed + warning fallback |
| Storage | Per-stream JSONL + cursors |
| Truth model | Slack authoritative in the gaps; local prefix assumed stable; heal on reconnect |
| Cursor model | Inbox: remote leads → `fetch` then `read`; Outbox: local leads → `write` then `flush` |
| Keys | `<channel_id>` or `<channel_id>:<thread_ts>` |
| Library | Thin stream API (sync + async); simple auto gap-close |
| CLI | Sync only; explicit read/write + pipe sugar; `fetch`/`flush` advanced |
| Not doing | Full Slack SDK layer; primary explicit-ack API; poll backend (yet) |
