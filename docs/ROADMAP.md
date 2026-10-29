# Roadmap

[简体中文](ROADMAP.zh-CN.md)

- **P0 Basics** (done; see `tests/e2e_p0.py` and `tests/e2e_astrbot.py`): plaintext direct chats and MUC group chats, OneBot v11 reverse WebSocket, heartbeat and lifecycle events, common actions, retracting the bot's own messages (XEP-0424), OOB links. Plaintext direct chat verified on a real Conversations client.
- **P1 Encryption** (in progress): ✅ OMEMO direct chats, send and receive (encrypted replies use `eu.siacs.conversations.axolotl`); ✅ blind trust before verification and manual trust; ✅ local and public-server end-to-end tests; ✅ encrypted interop verified on real mobile and desktop clients. To do: encrypted sending with `urn:xmpp:omemo:2` (needs SCE), encrypted key storage.
- **P2 Rich messages** (done): ✅ media send and receive (base64 / file / http sources, XEP-0363 upload + XEP-0066 OOB; XEP-0454 aesgcm encrypted upload and decrypted download in OMEMO chats); ✅ replies (XEP-0461 with XEP-0428 fallback quote, both directions, direct and group); ✅ automatic delivery receipts (XEP-0184); ✅ mentions (nick prefix + XEP-0372 reference, both directions); ✅ retraction (XEP-0424 for own messages; contacts' retractions arrive as `friend_recall` / `group_recall`); ✅ offline catch-up via MAM (XEP-0313, direct and group, deduplicated, original timestamps); ✅ undelivered events re-sent after the reverse WebSocket reconnects.
- **Phase one (P0–P2) is complete.** P3/P4 are on hold while the quality phase runs.
- **Quality phase** (in progress):
  - Round 1: static analysis (ruff, bandit, pip-audit: no high-severity findings). Manual audit fixes: SSRF protection for incoming media (public addresses only, re-checked at connect time against DNS rebinding, no redirects), a chunked-download truncation bug, a total size cap for the media directory, room invitations trusted only from roster contacts and admins by default, forged group messages from rooms the bot has not joined, MAM result origin checks, per-sender rate limit, truncation of overlong bodies, outgoing local files (non-regular files such as `/dev/zero` rejected, optional directory allow-list, `../` and symlink escapes blocked), base64 length checked before decoding, sanitized file extensions, background task errors no longer silent. Red-team test `e2e_redteam` 14/14; property and fuzz tests `test_props`; fault injection and load test `e2e_chaos` 5/5 (bot app outage queues and replays in order, reconnect and room rejoin after a server restart, 300 inbound messages in order at 62 msg/s, 200 outbound). SQLite in WAL mode (about 3× inbound throughput); message records pruned after `storage.keep_days`; single-instance lock per database (stops two instances sharing one account and OMEMO store); GitHub Actions CI that opens an issue with the log on failure. Public-server matrix: yax.im, jabber.vg, chat.between-us.online and lain.rocks messaging each other; direct chat, invite into a room, group chat, media and OMEMO all pass.
  - Round 2 (2026-10-19): mypy clean and enforced in CI (fixed 19 issues, including message segment types missing `record` / `video`). OMEMO session desync no longer loses messages for good: when a message cannot be decrypted the session is rebuilt and the sender gets an encrypted notice to resend (`e2e_omemo_resync` 8/8: first contact, duplicate delivery not decrypted twice, recovery after a lost session). Duplicate encrypted messages are dropped before decryption. Heartbeat task errors no longer show as "never retrieved". Single-instance lock works on Windows. 24-hour soak script `tests/soak.py` and manual client checklist in [TESTING.md](TESTING.md).
  - Round 3 (2026-10-26): `e2e_omemo_resync` 14/14 (adds a desynced ratchet and a contact who reinstalled their client with a new device and identity key). pip-audit of the project's own dependencies in a clean environment: no known vulnerabilities. End-to-end coverage 89% (`tests/cov_run.py`; client 89%, OneBot server 88%, OMEMO 96%). Fixed a shutdown race: `stop()` now stops message handling and catch-up first, waits for the stream to close, then closes the database; messages arriving during shutdown are left for the next MAM catch-up (this was the cause of an occasional lost first message in `e2e_mam` on CI).
  - 24-hour soak (2026-10-26 to 27, Debian 13 / Python 3.13.5, accounts on two public servers, one message a minute alternating OMEMO and plaintext): 1435 of 1440 answered; the 5 lost all fell in two windows when the test client itself reconnected. The gateway kept one XMPP session with 0 errors; RSS 84.9 → 86.8 MB, open files 16 → 16; database plus WAL reached about 4 MB in the first 4 hours (WAL full), then grew only 0.75 MB over the remaining 20. Round trip median 0.59 s, p95 0.83 s.
  - Manual client regression (2026-10-28): the full 10-item checklist was not run. Verified on real clients so far: plaintext and OMEMO direct chat commands on Conversations, OMEMO interop with a desktop client.
  - Pending real-client checks (automated tests pass, not yet tried on a real client): voice messages, recall notices for a contact's retraction, mentioned replies in a group, MAM catch-up after a gateway outage; all items on Gajim.
  - To do: recheck dependency vulnerabilities periodically (next: end of November 2026).
- **P3 More protocols** (on hold): Satori; OneBot v11 forward WebSocket and HTTP API; OneBot 12 as needed.
- **P4** (on hold): OMEMO in group chats (non-anonymous members-only rooms), multiple accounts, web UI, Docker images.

## Segment mapping

| OneBot v11 | XMPP |
|---|---|
| text | `<body>` |
| image / record / video / file | HTTP Upload + OOB |
| reply | XEP-0461 |
| at | MUC nick / XEP-0372 |
| delete_msg | XEP-0424 |
| friend / group lists | Roster / MUC bookmarks (XEP-0402) |
