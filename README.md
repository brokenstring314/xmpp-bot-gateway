# xmpp-bot-gateway

[![OneBot v11](https://img.shields.io/badge/OneBot-v11-black)](https://11.onebot.dev/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![CI](https://github.com/brokenstring314/xmpp-bot-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/brokenstring314/xmpp-bot-gateway/actions/workflows/ci.yml)

[简体中文](README.zh-CN.md)

**Run OneBot v11 chatbots on XMPP (Jabber).** Connect AstrBot, NoneBot2, LangBot or any other OneBot v11 bot framework to XMPP direct chats and group chats, with OMEMO encryption.

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
- Room invitations (XEP-0045 mediated and XEP-0249 direct) are accepted from roster contacts and `xmpp.admins` by default; set `xmpp.accept_invites` to `"all"` or `"none"` to change this

### Hardening

- Group messages are only accepted from rooms the bot has joined; archive results are only accepted from the bot's own archive or the room's.
- Incoming encrypted files are downloaded only from public addresses (checked again at connect time), without following redirects, with a size cap; the media directory has a total size cap.
- Per-sender rate limit and maximum message length for incoming messages.
- Outgoing local files must be regular files and can be restricted to `media.local_file_dirs`.
- Automatic XMPP reconnect; rooms are rejoined after reconnecting
- Incoming XMPP error stanzas are logged with their condition and text

### Supported actions

`send_private_msg`, `send_group_msg`, `send_msg`, `delete_msg`, `get_msg`, `get_login_info`, `get_status`, `get_version_info`, `can_send_image`, `can_send_record`, `get_friend_list`, `get_stranger_info`, `get_group_list`, `get_group_info`, `get_group_member_list`, `get_group_member_info`, `set_group_leave`.

Unsupported actions return `retcode 1404`. Failed actions return `status: "failed"` with a `wording` explaining why.

## Compatibility

Any OneBot v11 framework that accepts a reverse WebSocket connection should work. These were checked with `tests/e2e_framework.py` on a local Prosody server: a command sent from an XMPP user gets the framework's own reply, in a direct chat and in a group chat.

| Framework | Version | Reverse WebSocket URL | Notes |
|---|---|---|---|
| AstrBot | 4.29.3 | `ws://127.0.0.1:6199/ws` | OneBot v11 (aiocqhttp) adapter; also used for the OMEMO and public-server tests |
| LangBot | 4.11.2 | `ws://127.0.0.1:2280/ws` | OneBot v11 (aiocqhttp) adapter; by default it answers in groups only when the bot is mentioned |
| NoneBot2 | 2.5.0 (OneBot adapter 2.4.6) | `ws://127.0.0.1:8080/onebot/v11/ws` | `nonebot-adapter-onebot` with the FastAPI driver |

Checked on 2026-10-29. Not tested yet: OpenClaw, Hermes Agent (through community OneBot v11 plugins) and Koishi (needs Satori, which is not implemented).

## Quick start

```bash
uv venv && uv pip install -e .            # add ".[omemo]" for OMEMO support
cp config.example.toml config.toml   # set JID, password, rooms and the reverse WS URL
xmpp-gateway -c config.toml
```

In your bot framework, add a OneBot v11 adapter that listens for a reverse WebSocket connection, and put its URL in `onebot_v11.reverse_ws`.

## Docker

```bash
docker build -t xmpp-bot-gateway .        # --build-arg EXTRAS= leaves out the OMEMO dependencies
mkdir -p data && sudo chown 10001 data    # the container runs as uid 10001
docker run -d --name xmpp-gateway --restart unless-stopped \
  -v "$PWD/config.toml:/app/config.toml:ro" -v "$PWD/data:/app/data" xmpp-bot-gateway
```

The config file is read from `/app/config.toml`; the relative paths in it (`data/...`) resolve inside `/app`, so the database, OMEMO keys and media end up in the `data` volume. Back that directory up: it holds the bot's OMEMO identity. The gateway only makes outgoing connections. A framework running on the host is reachable with `--network host`, or at `ws://host.docker.internal:PORT/...` with `--add-host host.docker.internal:host-gateway`. `docker stop` shuts it down cleanly. `docker-compose.example.yml` has the same setup for Compose. Version tags (`v*`) publish amd64 and arm64 images to `ghcr.io/brokenstring314/xmpp-bot-gateway`.

## Configuration

Everything is set in one TOML file; `config.example.toml` lists every option with its default.

- `xmpp-gateway -c config.toml --check` validates the file and exits. Unknown keys are rejected, so a typo fails at startup instead of being silently ignored.
- Any string can read an environment variable with `${NAME}`, e.g. `password = "${XMPP_PASSWORD}"`, so secrets don't have to live in the file.
- `[log]` sets the level and an optional log file rotated by size.
- Each `onebot_v11.reverse_ws` entry is a URL or `{ url = "...", access_token = "..." }`, so several frameworks can connect with different tokens.
- Timeouts and backoffs (XMPP reconnect, room join, media transfer, OMEMO resync interval) are configurable too.

## Tests

```bash
tests/prosody/run.sh &           # local Prosody on 127.0.0.1:15222 with random test accounts
python3 -m pytest -q tests/test_cq.py tests/test_props.py tests/test_config.py   # unit, property-based and config tests
python3 -u tests/e2e_redteam.py  # forged, malformed and abusive stanzas from a hostile client
FRAMEWORK=astrbot python3 -u tests/e2e_redteam_chain.py  # hostile user against a real bot framework (see the file for setup)
python3 -u tests/e2e_chaos.py    # bot app outage, server restart, bursts in both directions (restarts Prosody)
python3 -u tests/e2e_p0.py       # end-to-end against a mock OneBot v11 app
python3 -u tests/e2e_media.py    # media upload/download, including OMEMO-encrypted files
python3 -u tests/e2e_reply.py    # replies, delivery receipts, mentions, recall notices
python3 -u tests/e2e_mam.py      # offline catch-up via MAM
```

`tests/e2e_framework.py` runs a direct and a group round trip against a running bot framework: set `FRAMEWORK_WS` to its reverse WebSocket URL, `CMD` to a command it answers (default `/help`), and optionally `GROUP_PREFIX` (e.g. `"bot: "` to mention the bot) and `EXPECT` (text the reply must contain).

`tests/e2e_omemo.py` checks OMEMO through a running AstrBot: an encrypted command gets an encrypted reply whose plaintext body is only the fallback text, and a following plaintext command gets a plaintext reply. `MODE=local` uses the local Prosody, `MODE=public` uses `.test_bots.json`.

`tests/e2e_public.py` runs against public XMPP servers: it reads test accounts from `.test_bots.json` (a list of `{"jid", "password"}`; pick roles with `BOT_JID` / `USER_JID`), then checks a direct chat, an invite into a freshly created room, and a group chat through a running AstrBot. Use accounts on your own server, or on a provider whose terms allow bot accounts, and keep test runs small.

## Status

Early development. See [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/TESTING.md](docs/TESTING.md).

## License

[Apache License 2.0](LICENSE).

The optional `[omemo]` extra installs `slixmpp-omemo` and `oldmemo`, which are licensed under AGPL-3.0-only. They keep their own license: if you distribute the gateway together with them, you also need to meet the AGPL-3.0 terms, and if you modify them and run the result as a network service, you must offer its users the modified source. The other dependencies use permissive licenses (MIT, BSD, Apache-2.0, PSF).

The published Docker image is built with the `[omemo]` extra, so it contains AGPL-3.0-only components and is distributed under the AGPL-3.0 terms as a whole; the code of this project inside it stays Apache-2.0. Both AGPL-3.0-only packages are pure Python and are installed unmodified, so their source code and license files are inside the image; their upstream sources are [slixmpp-omemo](https://github.com/Syndace/slixmpp-omemo) and [python-oldmemo](https://github.com/Syndace/python-oldmemo). Build with `--build-arg EXTRAS=""` for an image without them. The base image (`python:3.12-slim`, Debian) keeps the licenses of its own packages.

## Trademarks

This project is not affiliated with or endorsed by the XMPP Standards Foundation, the OneBot project, AstrBot, NoneBot or LangBot; their names are used only to describe compatibility. JABBER is a registered trademark and its use is licensed through the XMPP Standards Foundation.
