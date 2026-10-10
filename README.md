# slack_fd (`sfd`)

File-descriptor-like streams over Slack conversations: open a stream by key, `read` / `write` messages, and look back through durable append-only logs.

- Design: [ARCHITECTURE.md](ARCHITECTURE.md)
- Slack app + tokens: [SLACK_SETUP.md](SLACK_SETUP.md)

## Install (recommended: pipx)

One tool, isolated env, `sfd` on your PATH:

```bash
# once
python3 -m pip install --user pipx
pipx ensurepath
# restart the shell after ensurepath if `pipx` is not found

# from this repo (local checkout)
pipx install /path/to/slack_fd

# reinstall / upgrade after pulls
pipx install --force /path/to/slack_fd
```

Fish:

```fish
pipx install /path/to/slack_fd
```

Then:

```bash
sfd --help
```

**Why pipx?** Best fit for a CLI like `sfd`: no need to activate a venv every time, deps stay isolated, upgrades are one command. Plain `pip install --user .` also works but pollutes your user site-packages.

### Dev install (contributors)

```bash
cd slack_fd
python3.12 -m venv .venv
source .venv/bin/activate   # fish: source .venv/bin/activate.fish
pip install -e ".[dev]"
pytest
```

## Configure Slack tokens

```bash
# bash/zsh
export SLACK_BOT_TOKEN='xoxb-...'
export SLACK_APP_TOKEN='xapp-...'
```

```fish
set -gx SLACK_BOT_TOKEN 'xoxb-...'
set -gx SLACK_APP_TOKEN 'xapp-...'
```

See [SLACK_SETUP.md](SLACK_SETUP.md) (App Home → Messages Tab is required for DMs).

Optional: `SFD_DATA_DIR` (default `~/.local/share/sfd`).

## Quick start

```bash
sfd daemon start
sfd daemon status          # slack: true when tokens are set

# DM the bot in Slack, then:
sfd sync 'app:U_YOUR_USER_ID'
sfd log --chat 'app:U_YOUR_USER_ID'
sfd 'app:U_YOUR_USER_ID' --timeout 10
echo 'hello from sfd' | sfd 'app:U_YOUR_USER_ID'

sfd daemon stop
```

## IPC: Unix socket (default) vs local TCP

| Mode | When | Security |
|------|------|----------|
| **unix** (default) | Normal macOS/Linux | Socket file mode `0600` + data dir `0700` |
| **tcp** | Orgs that block Unix domain sockets | **Loopback only** (`127.0.0.1` / `::1`) + required auth token file |

```bash
# TCP localhost (set before daemon start; clients need the same env)
export SFD_IPC=tcp
export SFD_TCP_HOST=127.0.0.1          # default; non-loopback is rejected
export SFD_TCP_PORT=4173               # default; use 0 for ephemeral
sfd daemon start
```

Token is auto-created at `$SFD_DATA_DIR/daemon.token` (`0600`). Clients read it automatically from the data dir. Endpoint is recorded in `daemon.tcp`.

Do **not** bind to `0.0.0.0` — the daemon refuses non-loopback hosts.

## Library

```python
from slack_fd import SlackStream, StreamKey

with SlackStream("app:U01234567") as s:
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

## Status

Phases 0–3 complete (daemon, Socket Mode, sync, embed, trim, CLI sugar, optional TCP IPC).
