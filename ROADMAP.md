# Roadmap

Phases 0–4 are done (daemon, Socket Mode, fetch/flush, simple API, Slack-native keys).
This file tracks **deliberately deferred** work from the project’s north star:

> One Slack app; one daemon; streams keyed by Slack channel (and optional thread);
> tools `follow` / `read` / `write` only the scopes they own; names are for
> `ls` / `resolve`, not identity.

## Done (recent)

- [x] Slack-native keys: `C…` / `D…` / `G…` or `C…:thread_ts`
- [x] `sfd key -i|-o` for reply/follow keys
- [x] Single-daemon hardening (`daemon.lock`, refuse live-socket steal)
- [x] `sfd ls` (local) / `sfd ls --remote` / `sfd resolve`

## Next (when needed)

| Priority | Item | Notes |
|----------|------|--------|
| Ops polish | Live **leases** + `sfd listeners` | Track open `read`/`follow` waiters; cursors alone ≠ “listening”. Needed before “nobody’s listening” warnings in Slack. |
| Ops polish | Embed vs daemon Socket Mode | Fail hard if embed would steal the uplink while a daemon is up. |
| Product (v2) | **Listen / accept** | Channel supervisor → thread “connection” FDs. Add when there is a concrete @mention / multi-thread router — not before. |
| Policy | Message subtypes | Edits, deletes, `bot_message`, etc. Today: plain text; skip noisy subtypes. |
| Optional | Poll / `InboundSource` | Websocket is enough; history heal via `fetch`. Formal port only if a second backend appears. |

## Explicitly not planned soon

- Transparent ids on every `write` / `--print-key` — use `sfd key` instead
- Perfect POSIX FD isomorphism / `socat`-compatible byte streams
- Multi-language SDKs (other languages can spawn `sfd` or speak RPC later)
- SQLite indexing, protobuf IPC, Windows named pipes
- Full Slack surface (modals, oauth, files) — use Bolt beside `sfd` if needed
- One Slack app **per tool** — isolation is **which stream you open**

## Contract for tools

```text
Tool A:  follow D012…          # its DM with the bot
Tool B:  follow C99…:1710…     # one support thread
Tool C:  follow C99…           # top-level only in a channel
```

Same app token. Scope = stream key. Discover ids with `sfd resolve` / `sfd ls --remote`.
