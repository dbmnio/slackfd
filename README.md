# slack_fd (`sfd`)

File-descriptor-like streams over Slack conversations: open a stream by key, `read` / `write` messages, and look back through durable append-only logs.

- Design: [ARCHITECTURE.md](ARCHITECTURE.md)
- Slack app + tokens: [SLACK_SETUP.md](SLACK_SETUP.md)

## Model (short)

Inbox and outbox are not mirrors of the same op — they close gaps in opposite directions:

| Side | Gap | Close gap | Consume / produce |
|------|-----|-----------|-------------------|
| **Inbox** | remote ahead of local | `fetch` (Slack → local) | `read` advances local inbox cursor |
| **Outbox** | local ahead of remote | `flush` (local → Slack) | `write` advances local outbox cursor |

**Simple API (default):** `read` fetches when at the tip of known inbox, then advances the local cursor; `write` enqueues then flushes until sent.  
**Advanced:** `--no-fetch` / `--no-flush`, or `autofetch=False` / `autoflush=False`, and call `fetch` / `flush` yourself. (`sync` is a deprecated alias for `fetch`.)

## Install

Either **uv** or **pipx** puts `sfd` on your PATH in an isolated env. Prefer **uv** if you want the installer to fetch/manage the Python version as well (`requires-python >=3.11`).

### uv (recommended)

```bash
# once — https://docs.astral.sh/uv/
curl -LsSf https://astral.sh/uv/install.sh | sh

# from this repo (local checkout); uv installs a suitable Python if needed
uv tool install /path/to/slack_fd

# pin a version explicitly (optional)
uv tool install --python 3.12 /path/to/slack_fd

# reinstall / upgrade after pulls
uv tool install --force /path/to/slack_fd
# or: uv tool upgrade slack-fd
```

Ensure `uv tool`’s bin dir is on your `PATH` (the installer usually does this; see `uv tool dir --bin` if `sfd` is missing).

### pipx

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

Fish is the same once the tool is on `PATH`: `uv tool install …` or `pipx install …`.

Then:

```bash
sfd --help
```

Plain `pip install --user .` also works but pollutes your user site-packages.

### Dev install (contributors)

With uv:

```bash
cd slack_fd
uv venv --python 3.12
source .venv/bin/activate   # fish: source .venv/bin/activate.fish
uv pip install -e ".[dev]"
pytest
```

With venv + pip:

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
sfd daemon start           # exactly one; do not start from multiple shells/tools
sfd daemon status          # slack: true when tokens are set; warns if extra daemons

# DM the bot in Slack, then use the IM channel id (D…):
sfd 'D_YOUR_IM_CHANNEL' --timeout 10          # read (auto-fetch at tip)
echo 'hello from sfd' | sfd 'D_YOUR_IM_CHANNEL'  # write (auto-flush)
sfd log --chat 'D_YOUR_IM_CHANNEL'

# Reply/follow key from last inbox read (-i, default) or last sent outbox (-o)
sfd key 'D_YOUR_IM_CHANNEL'           # e.g. D…:ts to start/follow a thread
sfd key -o 'D_YOUR_IM_CHANNEL'        # requires a flushed send with Slack ts

# Discover stream ids (names are sugar; keys stay Slack ids)
sfd resolve '@alice'                  # → D… (opens IM)
sfd resolve '#general'                # → C…
sfd ls                                # local streams under the data dir
sfd ls --remote                       # conversations visible to the bot

# Advanced: manage gaps yourself
sfd fetch 'D_YOUR_IM_CHANNEL'
sfd flush 'D_YOUR_IM_CHANNEL'
sfd read --no-fetch 'D_YOUR_IM_CHANNEL' --timeout 10
echo 'queued' | sfd write --no-flush 'D_YOUR_IM_CHANNEL'

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

with SlackStream("D01234567") as s:
    msg = s.read(timeout=30)       # fetch if at tip; advance local inbox cursor
    if msg:
        s.write(f"echo: {msg.text}")  # outbox + flush

# Advanced gap management
with SlackStream("D01234567", autofetch=False, autoflush=False) as s:
    s.fetch()
    msg = s.read(timeout=0)
    s.write("queued")
    s.flush()
```

## Stream keys

| Key | Meaning |
|-----|---------|
| `C…` / `D…` / `G…` | Channel, IM, or MPIM (top-level tape) |
| `C…:1710000000.000100` | Thread under that channel (parent `ts`) |

## Status

Phases 0–4 complete (daemon, Socket Mode, fetch/flush, embed, trim, CLI sugar, TCP IPC, simple-API auto gap-close).  
Stream keys are Slack-native `<channel_id>[:thread_ts]`.  
Deferred work: [ROADMAP.md](ROADMAP.md).
