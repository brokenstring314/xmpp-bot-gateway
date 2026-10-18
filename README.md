# xmpp-bot-gateway

A standalone gateway that logs in to an XMPP account and exposes its direct chats and group chats (MUC) over the OneBot v11 protocol, so OneBot v11 bot frameworks can run on XMPP.

```
XMPP clients ⇄ XMPP server ⇄ xmpp-bot-gateway ⇄ OneBot v11 (reverse WebSocket) ⇄ bot framework
```

## Features

- Plaintext direct chats and MUC group chats (joins configured rooms, creates them as instant rooms if new, skips history replay)
- OneBot v11 reverse WebSocket (Universal role) with optional access token, auto-reconnect with backoff
- Meta events: `lifecycle` (connect) and `heartbeat`
- Message events: `message.private`, `message.group`; array or CQ-code string format
- Mentions: a group message starting with `<bot nick>:` arrives as an `at` segment; outgoing `at` segments become `<nick>: `
- Media: `image` / `record` / `video` / `file` segments with an http(s) URL are sent as a link with XEP-0066 OOB
- Retraction: `delete_msg` retracts messages sent by the gateway (XEP-0424)
- Stable numeric IDs for users, groups and messages, persisted in SQLite
- OMEMO for direct chats (optional, `[omemo]` extra): encrypted incoming messages are decrypted, and replies to a contact whose last message was encrypted are encrypted too, using legacy OMEMO (`eu.siacs.conversations.axolotl`); if encryption fails the send fails instead of falling back to plaintext. Trust: blind trust before verification (`btbv`) or `manual`. Group chat OMEMO is not supported yet; encrypted group messages are ignored
- Room invitations (XEP-0045 mediated and XEP-0249 direct) are accepted automatically; disable with `xmpp.accept_invites = false`
- Automatic XMPP reconnect; rooms are rejoined after reconnecting
- Incoming XMPP error stanzas are logged with their condition and text

### Supported actions

`send_private_msg`, `send_group_msg`, `send_msg`, `delete_msg`, `get_msg`, `get_login_info`, `get_status`, `get_version_info`, `can_send_image`, `can_send_record`, `get_friend_list`, `get_stranger_info`, `get_group_list`, `get_group_info`, `get_group_member_list`, `get_group_member_info`, `set_group_leave`.

Unsupported actions return `retcode 1404`. Failed actions return `status: "failed"` with a `wording` explaining why.

## Quick start

```bash
uv venv && uv pip install -e .            # add ".[omemo]" for OMEMO support
cp config.example.toml config.toml   # set JID, password, rooms and the reverse WS URL
xmpp-gateway -c config.toml
```

In your bot framework, add a OneBot v11 adapter that listens for a reverse WebSocket connection, and put its URL in `onebot_v11.reverse_ws`.

## Tests

```bash
tests/prosody/run.sh &           # local Prosody on 127.0.0.1:15222 with random test accounts
python3 -m pytest -q tests/test_cq.py
python3 -u tests/e2e_p0.py       # end-to-end against a mock OneBot v11 app
```

`tests/e2e_astrbot.py` runs the same round trip against a running AstrBot instance with a OneBot v11 adapter on `127.0.0.1:6199`.

`tests/e2e_omemo.py` checks OMEMO through a running AstrBot: an encrypted command gets an encrypted reply whose plaintext body is only the fallback text, and a following plaintext command gets a plaintext reply. `MODE=local` uses the local Prosody, `MODE=public` uses `.test_bots.json`.

`tests/e2e_public.py` runs against public XMPP servers: it reads test accounts from `.test_bots.json` (a list of `{"jid", "password"}`; pick roles with `BOT_JID` / `USER_JID`), then checks a direct chat, an invite into a freshly created room, and a group chat through a running AstrBot.

## Status

Early development. See [docs/ROADMAP.md](docs/ROADMAP.md).
