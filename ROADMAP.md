# Roadmap

Phases 0–4 delivered the working system. Direction for the **websocket daemon**
backend (current default):

> One Slack app; one daemon; streams keyed by Slack channel (and optional thread);
> tools `read` / `write` / `follow` only — daemon owns live demux, reconnect heal,
> and outbox send. `fetch` / `flush` are not the happy path for Socket Mode.

## Done (recent)

- [x] Slack-native keys: `C…` / `D…` / `G…` or `C…:thread_ts`
- [x] `sfd key -i|-o` for reply/follow keys
- [x] Single-daemon hardening (`daemon.lock`, refuse live-socket steal)
- [x] `sfd ls` / `sfd resolve`
- [x] Inbox dedupe by Slack `ts` (demux ↔ fetch races)
- [x] WS path: no autofetch on `read`; daemon flush-on-write; heal on Socket Mode connect

## Websocket backend focus

| Item | Notes |
|------|--------|
| Reconnect hooks | Heal again when Bolt/Socket Mode resumes after drop (today: heal on initial connect) |
| Flush reliability | Surface send failures; retry stuck `pending`/`failed` without client flush |
| Embed vs daemon | Fail hard if embed would steal Socket Mode while a daemon is up |
| Live **leases** / `sfd listeners` | Optional; needed before “nobody’s listening” warnings |

## Deferred (other backends / product)

| Item | Notes |
|------|--------|
| **Pull backend** + first-class `fetch`/`flush` | Makes sense when uplink *is* HTTP poll; not the Socket Mode FD surface |
| **Listen / accept** | Channel supervisor → thread FDs when a real @mention router needs it |
| Message subtypes | Edits, deletes, `bot_message`, … |
| Distribution | pip-only vs single binary later |

## Explicitly not planned soon

- Transparent ids on every `write` / `--print-key` — use `sfd key`
- Perfect POSIX FD isomorphism
- Multi-language SDKs
- SQLite / protobuf IPC / Windows named pipes
- Full Slack SDK surface — use Bolt beside `sfd` if needed

## Contract for tools

```text
Tool A:  follow D012…          # its DM with the bot
Tool B:  follow C99…:1710…     # one support thread
Tool C:  follow C99…           # top-level only in a channel
```

Same app token. Scope = stream key. Discover ids with `sfd resolve` / `sfd ls --remote`.
