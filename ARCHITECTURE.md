# slack_fd (`sfd`)

A file-descriptor-like interface to Slack conversations: open a stream by key, `read` / `write` messages, and look back through durable append-only logs. Backed by a lightweight daemon that owns the Socket Mode websocket.

This is **not** a Slack SDK replacement. The Slack Bolt/SDK remains the way to speak Slack’s protocol. This project only adds: demux/mux by conversation key, durable logs + cursors, a pull-based stream API, and a CLI that fits unix pipes.

---

## Goals

- **FD-like API** — open a logical stream, block on `read`, `write` outbound messages, replay history by offset.
- **No silent drops** — incoming and outgoing messages are appended to logs; consumers advance cursors.
- **Shared uplink** — one Socket Mode connection (daemon) shared by many CLI invocations and library clients.
- **Thin surface** — stream `read` / `write` / `follow`; the daemon owns live demux, heal-on-connect, and send-on-write. `fetch` / `flush` are admin/legacy (future pull backend), not the Socket Mode happy path.
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

`read` and `write` are **not** symmetric. For the **Socket Mode daemon** (default backend):

```text
INBOX (messages originate remotely)
  local_inbox_cursor ──────────────► tip of local inbox log
                     │ daemon demux (live WS) fills the log │
                     │ heal-on-connect backfills gaps       │
                     │ read() / follow() consume + advance  │

OUTBOX (messages originate locally)
  remote Slack tip ◄────────────── local outbox log
                     │ write() enqueues (durable)           │
                     │ daemon send-on-write closes the gap  │
```

| Op | Who | Meaning |
|----|-----|---------|
| `read` / `follow` | Client | Consume next known inbox message; advance local cursor |
| `write` | Client → daemon | Enqueue outbound; daemon sends when Slack is up |
| demux / heal-on-connect | Daemon | Live WS + history backfill into inbox (`ts` deduped) |
| send-on-write | Daemon | `chat.postMessage` for pending outbox (`repair_first=False` on hot path) |
| `fetch` / `flush` | Admin / legacy | Force heal or drain; primary API for a **future pull** backend, not WS happy path |

### Assumptions about prefixes

| Prefix | Assumption | Realistic drift |
|--------|------------|-----------------|
| Inbox ≤ local cursor | Already-read history is correct | Edits/deletes of old msgs (uncommon for bot DMs). |
| Outbox ≤ remote cursor | Slack-acked sends are correct | Crash pain lives in the gap (sent remotely but still `pending` locally) → heal-on-connect / admin repair. |

Slack is the authority for **what exists in a conversation**. Local logs are a cache, spool, and multi-cursor tape. Healing reconciles **gaps**, not the whole history every read.

### Naming: `fetch`, not `sync`

`fetch` = Slack → local history heal (admin). It is **not** bidirectional.  
Outbound drain is daemon send-on-write (or admin `flush`).  
(`sfd sync` remains a deprecated CLI alias for `fetch`.)

Defaults: client ``autofetch=False``; flush ``repair_first=False``.

---

## Client surface vs daemon (Socket Mode)

### Happy path (default)

```text
read() / follow()     # local inbox only; daemon demux fills it
write(text)           # durable outbox; daemon sends when connected
```

### Admin / legacy (and future pull backend)

```text
fetch()               # force conversations.history / replies into inbox
flush()               # force drain pending outbox (optional repair_first=True)
```

| Surface | Socket Mode (now) | Pull backend (later) |
|---------|-------------------|----------------------|
| Inbox catch-up | Daemon demux + heal-on-connect | Client / daemon `fetch` |
| Outbox drain | Daemon send-on-write | Explicit `flush` |
| Cursor on read | Advances on `read` | same |

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
- History heal uses Web API on Socket Mode connect (and admin `fetch`); not on every client `read`.

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
- **Single daemon only.** Concurrent `daemon start` used to race (TOCTOU + unlinking a live Unix socket), spawning multiple `python -m slack_fd.daemon` processes and breaking Socket Mode. `start` is now serialized with `daemon.lock` (flock); a second process refuses to bind if the socket already accepts connections. `sfd daemon status` warns if extra daemon PIDs are detected. Examples (e.g. Slack↔Cursor bridge) must use `transport=daemon` and **never** start their own daemon.

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
- Inbox appends dedupe by Slack `ts` (demux retries + fetch races).
- Outbox crash-in-gap: heal-on-connect and optional admin `flush --repair`.

### AD-6 — Library scope: thin stream API only

Supported concepts:

- Stream keys (`C…` / `D…` / `G…` or `C…:thread_ts`)
- Happy path: `read` / `write` / `follow` / `read_at` / `log` / `chat` view
- Admin/legacy: `fetch` / `flush` (pull backend later)
- Daemon lifecycle helpers

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
    msg = s.read()              # local inbox; daemon demux fills it
    msg = s.read(timeout=30)    # None on timeout
    s.write("hello")            # durable outbox; daemon sends when up
    past = s.read_at(0)         # absolute offset; does not move cursor
    for msg in s.follow():      # block and yield forever
        ...
```

### Admin / legacy (force heal or drain)

```python
with SlackStream(key, autofetch=False, autoflush=False) as s:
    s.fetch()                   # force history heal
    msg = s.read(timeout=0)
    s.write("hello")            # enqueue only if transport left pending
    s.flush()                   # force drain (repair_first=False by default)
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

# Write (stdin → stream) — daemon sends when Slack is up
echo "hello" | sfd write C012:1710000000.000100
echo "hello" | sfd D01234567             # sugar: write if stdin is a pipe

# Read next (stdout) — local inbox; daemon demux fills it
sfd read C012:1710000000.000100
sfd D01234567                            # sugar: read if tty

# Admin/legacy (not the Socket Mode happy path)
sfd fetch D012…                          # force history heal
sfd flush D012…                          # force outbox drain
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

| Command | Direction | Role |
|---------|-----------|------|
| `sfd fetch` | Slack → local | Admin: force history heal (daemon also heals on connect) |
| `sfd flush` | Local → Slack | Admin: force drain pending outbox (daemon sends on write) |
| `sfd sync` | — | Deprecated alias for `fetch` |

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
2. Connect Socket Mode; auto-reconnect (Bolt); heal local streams on connect.
3. On event: ack promptly → demux → append inbox JSONL (`ts` deduped) → notify waiters.
4. On client write RPC: append outbox → send via Web API when Slack is up (`repair_first=False`).
5. Serve read/follow/log (+ admin fetch/flush) RPCs from logstore.
6. PID file + socket path; graceful shutdown; single-instance flock on start/stop.

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

- Bolt Socket Mode in the daemon; demux to `C…`/`D…`/`G…` or `C…:thread_ts`; outbox send via Web API.
- Auto falls back to embed + warning if daemon down.
- End-to-end: real Slack ↔ `sfd read`/`write` when tokens are configured.

### Phase 3 — Async + polish ✅

- ✅ `AsyncSlackStream` via `asyncio.to_thread` over the sync client.
- ✅ Merged chat *view* (`history()` / `sfd log --chat`) — physical inbox/outbox stay separate.
- ✅ Chat table formatting for `--chat`.
- ✅ Fetch/flush, embed, trim, CLI key/pipe sugar, optional TCP IPC.

### Phase 4 — Simple API semantics ✅

Original gap API (`autofetch` / client `flush`) shipped; **websocket-daemon direction** now:

| Behavior | Owner |
|----------|--------|
| Live inbox | Daemon Socket Mode demux (`ts` deduped) |
| Heal after connect | Daemon `reconcile_after_connect` (history + drain) |
| Send | Daemon flush-on-`write` (no history repair on hot path) |
| Client `read` / `write` | Consume / enqueue only — no happy-path `fetch`/`flush` |

`sfd fetch` / `sfd flush` remain as **admin/legacy** RPCs (and for a future pull backend). They are not the FD surface for Socket Mode.

Non-goals: continuous bidirectional mirror; rewriting inbox behind the cursor; client-driven heal on every tip read.

---

## Open questions (deferred, not blocking)

See [ROADMAP.md](ROADMAP.md) for the living backlog. Highlights:

1. Message subtype filtering (bot_message, message_changed, deletes)? (**v1: store text messages; skip noisy subtypes or store with flags**)
2. Distribution: pip-only vs eventual Go rewrite for single binary.
3. ~~Name → channel_id resolve helpers (`sfd ls` / `resolve`).~~ ✅
4. Listen/accept for thread discovery under a channel supervisor (v2; park until a real product needs it).
5. Live listener leases / `sfd listeners` (required before “nobody’s listening” warnings).

---

## Summary

| Decision | Choice |
|----------|--------|
| Language | Python + official Bolt |
| Inbound | Socket Mode demux; daemon heal on connect |
| Outbound | Web API send on daemon `write` (durable outbox) |
| Process | Daemon default; embed + warning fallback |
| Storage | Per-stream JSONL + cursors |
| Truth model | Slack authoritative in the gaps; local prefix assumed stable; heal on reconnect |
| Cursor model | Inbox: demux + heal-on-connect → `read`; Outbox: `write` → daemon send |
| Keys | `<channel_id>` or `<channel_id>:<thread_ts>` |
| Library | Thin stream API (sync + async); `autofetch=False`; send-on-write in daemon |
| CLI | Sync only; read/write/follow + pipe sugar; `fetch`/`flush` admin/legacy |
| Not doing | Full Slack SDK layer; client fetch/flush as happy path; poll backend (yet) |
