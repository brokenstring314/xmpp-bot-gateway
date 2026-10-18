# xmpp-bot-gateway

A standalone gateway that logs in to an XMPP account and exposes its direct chats and group chats (MUC) over the OneBot v11 protocol, so OneBot v11 bot frameworks can run on XMPP.

```
XMPP clients ⇄ XMPP server ⇄ xmpp-bot-gateway ⇄ OneBot v11 (reverse WebSocket) ⇄ bot framework
```

## Features

- Plaintext direct chats and MUC group chats (joins configured rooms, creates them as instant rooms if new; history replay on join is skipped, missed messages come from MAM catch-up instead)
- OneBot v11 reverse WebSocket (Universal role) with optional access token, auto-reconnect with backoff
- Meta events: `lifecycle` (connect) and `heartbeat`
- Message events: `message.private`, `message.group`; array or CQ-code string format
- Mentions: a group message starting with `<bot nick>:`, or carrying an XEP-0372 mention reference to the bot, arrives as an `at` segment; outgoing `at` segments become `<nick>: ` with an XEP-0372 mention reference
- Outgoing media: `image` / `record` / `video` / `file` segments accept an http(s) URL, `base64://` data or a `file://` / local path. Non-URL data is uploaded with XEP-0363 HTTP Upload. Each media item is sent as its own message whose body is the link, with XEP-0066 OOB, so clients show it inline. In an OMEMO chat the file is encrypted before upload (XEP-0454) and sent as an encrypted `aesgcm://` link; no plaintext OOB is attached
- Incoming media: a message whose body is a single http(s) link matching its OOB URL, or an `aesgcm://` link in an OMEMO chat, becomes an `image` / `record` / `video` segment (chosen by file extension). Encrypted files are downloaded, decrypted and saved under `media.dir`, and passed on as a `file://` URI, so the bot framework must be able to read that path
- Replies (XEP-0461): an outgoing `reply` segment references the original message (in group chats by the room's stanza-id) and adds a quoted fallback line (XEP-0428) for clients without reply support; an incoming reply becomes a `reply` segment with the original `message_id` and the quoted fallback is removed from the text. If the original message is unknown, the quote is kept as text
- Delivery receipts (XEP-0184): receipt requests on incoming messages are answered automatically
- Retraction: `delete_msg` retracts messages sent by the gateway (XEP-0424), including every part of a message that was split into text and media. When a contact or room occupant retracts one of their own messages, a `notice.friend_recall` / `notice.group_recall` event is sent with the original `message_id`; retractions of other people's or unknown messages are ignored
- Offline catch-up (XEP-0313 MAM): after connecting or reconnecting, direct and group messages missed while offline are fetched from the server archives and delivered with their original timestamps, without duplicates. The first run starts from the current time; limits are `xmpp.mam_max_messages` per archive and `xmpp.mam_max_age` seconds; disable with `xmpp.mam_catchup = false`
- Undelivered events are re-sent after the OneBot connection is re-established
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
python3 -u tests/e2e_media.py    # media upload/download, including OMEMO-encrypted files
python3 -u tests/e2e_reply.py    # replies, delivery receipts, mentions, recall notices
python3 -u tests/e2e_mam.py      # offline catch-up via MAM
```

`tests/e2e_astrbot.py` runs the same round trip against a running AstrBot instance with a OneBot v11 adapter on `127.0.0.1:6199`.

`tests/e2e_omemo.py` checks OMEMO through a running AstrBot: an encrypted command gets an encrypted reply whose plaintext body is only the fallback text, and a following plaintext command gets a plaintext reply. `MODE=local` uses the local Prosody, `MODE=public` uses `.test_bots.json`.

`tests/e2e_public.py` runs against public XMPP servers: it reads test accounts from `.test_bots.json` (a list of `{"jid", "password"}`; pick roles with `BOT_JID` / `USER_JID`), then checks a direct chat, an invite into a freshly created room, and a group chat through a running AstrBot.

## Status

Early development. See [docs/ROADMAP.md](docs/ROADMAP.md).
