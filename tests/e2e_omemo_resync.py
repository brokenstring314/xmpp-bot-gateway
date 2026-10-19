"""OMEMO robustness without an external bot framework (mock OneBot app):

  1. first contact: a fresh user device sends an encrypted message -> the app gets it, the reply is encrypted
  2. duplicate delivery of the same encrypted stanza (offline storage + MAM) -> one event, no decrypt attempt
  3. broken session (the bot lost its session with the user, e.g. restored an old backup) -> the bot
     rebuilds the session and tells the user, and the user's next message gets through

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_omemo_resync.py`.
"""
import asyncio, json, logging, os, shutil, sys

import slixmpp
import tomli_w
from aiohttp import web
from slixmpp.jid import JID

sys.path[:0] = ["src", "tests"]
from harness import MockApp, check, results, settle, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402
from xmpp_gateway.xmpp import omemo as omemo_mod  # noqa: E402

PORT = 6297
RUN = "tests/prosody/run"
WORK = "data/e2e_omemo_resync"
NS_OLD = "eu.siacs.conversations.axolotl"


class Counter(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def count(self, s):
        return sum(s in x for x in self.lines)


class OmemoUser(slixmpp.ClientXMPP):
    def __init__(self, jid, pw, bot_jid):
        super().__init__(jid, pw)
        self.bot_jid = bot_jid
        self.use_message_ids = True
        self.register_plugin("xep_0380")
        self.register_plugin("xep_0384", {"store_path": f"{WORK}/user.json", "btbv": True}, module=omemo_mod)
        self.ready = asyncio.Event()
        self.inbox: list[tuple[str, str]] = []    # (kind, plaintext) kind = enc | empty | plain
        self.add_event_handler("session_start", self._start)
        self.add_event_handler("message", self._on_msg)

    async def _start(self, _):
        self.send_presence()
        await self.get_roster()
        await self.plugin["xep_0384"].get_session_manager()
        self.ready.set()

    async def _on_msg(self, m):
        if m["type"] != "chat" or m["from"].bare != self.bot_jid:
            return
        if self.plugin["xep_0384"].is_encrypted(m):
            try:
                dec, _ = await self.plugin["xep_0384"].decrypt_message(m)
            except Exception as e:  # noqa: BLE001
                self.inbox.append(("fail", repr(e)))
                return
            self.inbox.append(("enc", dec["body"]) if dec["body"] else ("empty", ""))
        elif m["body"]:
            self.inbox.append(("plain", m["body"]))

    async def encrypted(self, text) -> slixmpp.Message:
        msg = self.make_message(mto=self.bot_jid, mbody=text, mtype="chat")
        enc, errors = await self.plugin["xep_0384"].encrypt_message(msg, JID(self.bot_jid))
        assert not errors, errors
        enc["eme"]["namespace"], enc["eme"]["name"] = NS_OLD, "OMEMO"
        return enc


def texts(app, k):
    return [e["raw_message"] for e in app.events[k:]]


async def main():
    logging.basicConfig(level=logging.ERROR)
    counter = Counter()
    logging.getLogger("xmpp_gateway").addHandler(counter)
    logging.getLogger("xmpp_gateway").setLevel(logging.INFO)
    shutil.rmtree(WORK, ignore_errors=True)          # fresh devices on both sides: true first contact
    os.makedirs(WORK)
    acc = json.load(open(f"{RUN}/accounts.json"))
    bot_jid, user_jid = acc["bot"]["jid"], acc["user"]["jid"]
    cfg = {"xmpp": {**acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot",
                    "rooms": [], "rate_limit": 0, "mam_catchup": False},
           "omemo": {"enabled": True, "trust": "btbv", "store_dir": f"{WORK}/bot"},
           "storage": {"db": f"{WORK}/gateway.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())

    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    r = web.AppRunner(wa)
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", PORT).start()

    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws, 20)
    await settle(app)

    u = OmemoUser(user_jid, acc["user"]["password"], bot_jid)
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u.plugin["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(u.ready.wait(), 20)
    u.send_presence_subscription(pto=bot_jid)
    await asyncio.sleep(2)
    await u.plugin["xep_0384"].refresh_device_lists({JID(bot_jid)}, force_download=True)
    await settle(app)

    # 1. first contact
    k = len(app.events)
    first = await u.encrypted("hello-1")
    first_xml = str(first)
    first.send()
    ev = await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == "hello-1"), None), 20)
    check("first contact: app receives the decrypted message", ev, texts(app, k))
    if ev:
        await app.call("send_private_msg", user_id=ev["user_id"], message="reply-1")
    got = await until(lambda: ("enc", "reply-1") in u.inbox, 20)
    check("first contact: reply comes back encrypted", got, u.inbox)

    # 2. the very same stanza arrives again (offline storage and MAM both hold it)
    k, fails = len(app.events), counter.count("OMEMO decrypt failed")
    u.send_raw(first_xml)
    await asyncio.sleep(3)
    check("duplicate stanza: no second event", "hello-1" not in texts(app, k), texts(app, k))
    check("duplicate stanza: not decrypted again", counter.count("OMEMO decrypt failed") == fails, counter.lines[-3:])

    # 3. the bot loses its session with the user (old backup restored, store damaged ...)
    await gw.stop()
    store_file = next(p for p in os.listdir(f"{WORK}/bot") if p.endswith(".json"))
    store_path = f"{WORK}/bot/{store_file}"
    data = json.load(open(store_path))
    dropped = [key for key in data if key.startswith(f"/{NS_OLD}/{user_jid}/")]   # sessions with the user
    for key in dropped:
        del data[key]
    json.dump(data, open(store_path, "w"))
    check("test setup: bot session with the user removed", dropped, list(data)[:20])
    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws, 20)
    await asyncio.sleep(3)

    k, n_in = len(app.events), len(u.inbox)
    (await u.encrypted("lost-2")).send()
    note = await until(lambda: next((m for m in u.inbox[n_in:] if m[0] == "enc"), None), 20)
    check("broken session: user is told to resend (encrypted)", note, u.inbox[n_in:])
    check("broken session: undecryptable message not forwarded", "lost-2" not in texts(app, k))
    await asyncio.sleep(1)
    (await u.encrypted("again-3")).send()
    ev = await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == "again-3"), None), 20)
    check("broken session: next message gets through", ev, (texts(app, k), counter.lines[-3:]))

    await gw.stop()
    ok = sum(results.values())
    print(f"RESULT {ok}/{len(results)} passed", flush=True)
    os._exit(0 if ok == len(results) else 1)

asyncio.run(main())
