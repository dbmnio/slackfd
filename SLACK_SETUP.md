# Setting up a Slack app for `sfd`

`sfd` uses **Socket Mode**: your daemon opens a websocket to Slack (no public HTTPS URL required). You need two tokens:

| Token | Prefix | Used for |
|-------|--------|----------|
| **Bot token** | `xoxb-` | Web API calls (send messages, open DMs, etc.) |
| **App-level token** | `xapp-` | Opening the Socket Mode websocket (`connections:write`) |

Official docs: [Using Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/).

---

## 1. Create a Slack app

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and sign in.
2. Click **Create New App** → **From scratch**.
3. Name it (e.g. `sfd`) and pick the workspace you’ll develop in.
4. Create the app.

---

## 2. Enable Socket Mode

1. In the left sidebar: **Settings → Socket Mode**.
2. Toggle **Enable Socket Mode** **On**.
3. Slack will prompt you to create an **App-Level Token** (or do it under **Settings → Basic Information → App-level tokens**):
   - Token name: e.g. `sfd-socket`
   - Scope: **`connections:write`** (required)
   - Click **Generate**
4. Copy the token — it starts with `xapp-`. This is your **`SLACK_APP_TOKEN`**.
   - Store it somewhere safe; you may not see the full value again easily.

You can also create the app-level token later from **Basic Information → App-level tokens** if you skipped the prompt.

---

## 3. Add bot token scopes

1. **Features → OAuth & Permissions**.
2. Under **Scopes → Bot Token Scopes**, add:

### Required for day-1 `sfd` (thread / DM / app DM)

| Scope | Why |
|-------|-----|
| `chat:write` | Send messages (`sfd write`) |
| `im:history` | Receive DM message events / history |
| `im:read` | Resolve IM channel metadata |
| `im:write` | Open a DM channel with a user |
| `channels:history` | Receive messages in public channels the bot is in |
| `channels:read` | Channel metadata |
| `groups:history` | Private channels the bot is invited to |
| `groups:read` | Private channel metadata |
| `mpim:history` | Multi-person DMs |
| `mpim:read` | MPIM metadata |
| `users:read` | Look up users (helpful for DM keys) |
| `app_mentions:read` | Optional but useful when mentioned in channels |

### Usually *not* required for v1

| Scope | Notes |
|-------|--------|
| `chat:write.public` | Post to public channels *without* joining — only if you want that |
| `files:read` / `files:write` | File attachments — add later if needed |
| `commands` | Slash commands — out of scope for thin stream API |

Reinstall the app after any scope change (step 5).

---

## 4. Subscribe to bot events

Socket Mode still needs **Event Subscriptions** enabled (no Request URL when Socket Mode is on).

1. **Features → Event Subscriptions**.
2. Toggle **Enable Events** **On**.
3. Under **Subscribe to bot events**, add:

| Event | Why |
|-------|-----|
| `message.im` | DMs with the bot (`dm:` / `app:` streams) |
| `message.channels` | Messages in public channels the bot is in |
| `message.groups` | Messages in private channels the bot is in |
| `message.mpim` | Multi-person DMs |
| `app_mention` | Optional; @mentions in channels |

Save changes. You’ll need to reinstall if prompted.

---

## 5. Allow users to message the bot (App Home)

New apps often show **“Sending messages to this app has been turned off”** in the bot DM until you enable the Messages tab.

1. **Features → App Home**.
2. Under **Show Tabs**, turn on **Messages Tab**.
3. Check **Allow users to send Slash commands and messages from the messages tab**.
   - In the manifest this is `messages_tab_enabled: true` and `messages_tab_read_only_enabled: false`.

You also need `chat:write` (already in the scope list) and the `message.im` event (already in the events list) for the bot to receive those DMs.

---

## 6. Install the app to your workspace

1. **Settings → Install App** (or **OAuth & Permissions → Install to Workspace**).
2. Review permissions → **Allow**.
3. Copy the **Bot User OAuth Token** — it starts with `xoxb-`. This is your **`SLACK_BOT_TOKEN`**.

If you change scopes or events later: **Reinstall to Workspace** and use the (possibly refreshed) bot token.

---

## 7. Invite the bot where you want it

- **Channels:** `/invite @YourBotName` (or add via channel members UI). Without this, you won’t see `message.channels` / can’t post with only `chat:write`.
- **DMs:** Open the app → **Messages** tab (or find the bot under Apps / DMs) and send a message.
- **Private channels:** A member must invite the bot.

---

## 8. Configure tokens for `sfd`

### Environment variables (recommended for trying things out)

```bash
export SLACK_BOT_TOKEN='xoxb-...'
export SLACK_APP_TOKEN='xapp-...'
```

### Config file (optional)

`~/.local/share/sfd/config.toml` (or `$SFD_DATA_DIR/config.toml`):

```toml
bot_token = "xoxb-..."
app_token = "xapp-..."
```

**Do not commit tokens** to git. Treat the data directory as sensitive (message logs live there too).

---

## 9. Verify the pieces

| Check | How |
|-------|-----|
| Bot token works | Slack API tester or any `auth.test` call with `xoxb-` |
| App token works | Daemon connects in Socket Mode without auth errors (Phase 2+) |
| Events flow | DM the bot; daemon should append to an `app:<user_id>` / `dm:<user_id>` inbox |
| Sends work | `sfd write dm:U… "hi"` (once Slack mux is wired) |

With tokens set, `sfd daemon start` opens Socket Mode. Then DM the bot and `sfd read 'app:<your_user_id>'`. Without tokens, use `sfd inject` for local-only testing.

---

## App manifest (optional shortcut)

You can create/update the app from a manifest (**App Manifest** in settings). Example YAML aligned with day-1 needs:

```yaml
display_information:
  name: sfd
  description: File-descriptor-like Slack streams
features:
  bot_user:
    display_name: sfd
    always_online: true
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - channels:read
      - chat:write
      - groups:history
      - groups:read
      - im:history
      - im:read
      - im:write
      - mpim:history
      - mpim:read
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
      - message.groups
      - message.im
      - message.mpim
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

After applying a manifest: create the **app-level token** with `connections:write`, then **Install to Workspace**, and copy both tokens.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Socket Mode won’t connect | Missing/invalid `xapp-` token or missing `connections:write` |
| `invalid_auth` on send | Bad/revoked `xoxb-` token; reinstall app |
| No inbound channel messages | Bot not invited; missing `message.channels` / `channels:history` |
| No DMs / “Sending messages to this app has been turned off” | **App Home → Messages Tab** not enabled, or “Allow users to send … messages” unchecked; also check `message.im` / `im:*` |
| Two connections fighting | Two daemons or daemon + embed both using Socket Mode — only one active connection per app |
| Scopes “not working” | Forgot to **reinstall** after changing scopes/events |

---

## Security notes

- Prefer a **dev workspace** while building.
- Restrict who can install the app in production workspaces.
- Rotate tokens if leaked (revoke app-level token; reinstall for bot token).
- Socket Mode traffic is still Slack-hosted; local risk is mainly tokens + JSONL logs under the data dir (`0600` socket / `0700` dirs in `sfd`).
