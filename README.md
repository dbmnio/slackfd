# slack_fd (`sfd`)

File-descriptor-like streams over Slack conversations: open a stream by key, `read` / `write` messages, and look back through durable append-only logs.

- Design: [ARCHITECTURE.md](ARCHITECTURE.md)
- Slack app + tokens (scopes, Socket Mode, `xoxb` / `xapp`): [SLACK_SETUP.md](SLACK_SETUP.md)

**Phase 2 (current):** daemon + Unix socket RPC + JSONL logs + Slack Socket Mode demux/mux + sync library + CLI.

## Install

```bash
cd slack_fd
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configure Slack tokens

```bash
export SLACK_BOT_TOKEN='xoxb-...'
export SLACK_APP_TOKEN='xapp-...'
# optional: export SFD_DATA_DIR=~/.local/share/sfd
```

See [SLACK_SETUP.md](SLACK_SETUP.md) (including App Home → Messages Tab).

## Live test (bot DM)

```bash
sfd daemon start
sfd daemon status   # expect "slack": true

# In Slack: DM the bot any message

# Find your user id (U…) from the log, or use profile → Copy member ID
sfd log 'app:U_YOUR_USER_ID'
sfd log --chat 'app:U_YOUR_USER_ID'   # merged timeline; logs stay separate
sfd read 'app:U_YOUR_USER_ID' --timeout 5

# Reply as the bot
echo 'hello from sfd' | sfd write 'app:U_YOUR_USER_ID'

sfd daemon stop
```

`dm:U…` and `app:U…` both receive the same inbound IM (two stream aliases). Channel threads use `thread:<channel>:<thread_ts>` (bot must be invited).

Without tokens, the daemon still runs; use `sfd inject` for local testing.

## Library

```python
from slack_fd import SlackStream, StreamKey

key = StreamKey.parse("app:U01234567")

with SlackStream(key) as s:  # talks to daemon when running
    msg = s.read(timeout=30)
    if msg:
        s.write(f"echo: {msg.text}")
```

## Stream keys

| Key | Meaning |
|-----|---------|
| `thread:<channel_id>:<thread_ts>` | Channel thread |
| `dm:<user_id>` | IM with a user |
| `app:<user_id>` | User ↔ bot DM |

## Tests

```bash
pytest
```

## Status

| Phase | Status |
|-------|--------|
| 0 Skeleton | done |
| 1 Daemon + UDS IPC | done |
| 2 Slack Socket Mode | **done** |
| 3 Async + embed polish | planned |
