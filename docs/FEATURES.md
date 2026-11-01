# Feature checklist

[简体中文](FEATURES.zh-CN.md)

What the gateway supports today, and what it does not support yet. Items marked "Not supported yet" are listed so you can see the gaps; listing them is not a promise that they will be added. Details of each supported feature are in the [README](../README.md#features).

✅ Supported · 🟡 Partly supported · ⬜ Not supported yet

## Direct chats

| Feature | Status |
|---|---|
| Send and receive text (array or CQ-code string format) | ✅ |
| OMEMO encryption (legacy `eu.siacs.conversations.axolotl`), blind trust or manual trust, session recovery | ✅ |
| OMEMO `urn:xmpp:omemo:2` for sending | ⬜ Not supported yet |
| Encrypted storage of OMEMO keys (stored as mode-600 JSON today) | ⬜ Not supported yet |
| Images, voice, video and files, both directions (HTTP Upload + OOB; `aesgcm://` in OMEMO chats) | ✅ |
| Replies (XEP-0461) | ✅ |
| Retracting the bot's own messages; recall notices for a contact's retraction | ✅ |
| Delivery receipts (XEP-0184) | ✅ |
| Offline catch-up from the server archive (XEP-0313 MAM) | ✅ |
| Contact requests: accepted automatically, not reported to the bot framework | 🟡 |
| Contact request events and accept / reject / remove actions | ⬜ Not supported yet |
| Message corrections (XEP-0308) | ⬜ Not supported yet |
| Reactions (XEP-0444) | ⬜ Not supported yet |
| Typing notifications (XEP-0085) | ⬜ Not supported yet |

## Group chats (MUC)

| Feature | Status |
|---|---|
| Join configured rooms, create new rooms, rejoin after reconnecting | ✅ |
| Room invitations (XEP-0045 and XEP-0249), trusted senders configurable | ✅ |
| Send and receive group messages, mentions, replies, media | ✅ |
| Recall notices, offline catch-up from the room archive | ✅ |
| Stable member identity (real JID or XEP-0421 occupant-id) | ✅ |
| Group list, group info, member list, member info, leaving a group | ✅ |
| OMEMO in group chats (encrypted group messages are ignored) | ⬜ Not supported yet |
| Member join / leave notices (`group_increase` / `group_decrease`) | ⬜ Not supported yet |
| Kicking members (`set_group_kick`) | ⬜ Not supported yet |
| Muting a member or the whole room (`set_group_ban` / `set_group_whole_ban`) | ⬜ Not supported yet |
| Setting admins (`set_group_admin`) and admin change notices | ⬜ Not supported yet |
| Changing the bot's nickname or the room name / subject (`set_group_card` / `set_group_name`) | ⬜ Not supported yet |
| Invitation requests reported to the framework (`request.group` / `set_group_add_request`) | ⬜ Not supported yet |
| Private messages with a room member as a separate conversation | ⬜ Not supported yet |

## Bot protocols

| Feature | Status |
|---|---|
| OneBot v11 reverse WebSocket (several endpoints, per-endpoint token, heartbeat, lifecycle, re-sending missed events) | ✅ |
| OneBot v11 forward WebSocket | ⬜ Not supported yet |
| OneBot v11 HTTP API and HTTP POST events | ⬜ Not supported yet |
| Satori | ⬜ Not supported yet |
| OneBot 12 | ⬜ Not supported yet |
| OneBot v11 actions with no XMPP counterpart (forwarded messages, pokes, likes and similar) | ⬜ Not supported yet |

## Deployment and operation

| Feature | Status |
|---|---|
| TOML config, environment variables, `--check` validation, rotating logs | ✅ |
| Docker image (amd64 / arm64), clean shutdown on SIGTERM | ✅ |
| SQLite storage with pruning, single-instance lock, rate limit, body length limit | ✅ |
| Web UI for configuration | ⬜ Not supported yet |
| Several XMPP accounts in one process | ⬜ Not supported yet |
| Stream Management (XEP-0198) | ⬜ Not supported yet |
| Bot avatar and profile (vCard / XEP-0084) | ⬜ Not supported yet |
| Health check endpoint or metrics | ⬜ Not supported yet |
