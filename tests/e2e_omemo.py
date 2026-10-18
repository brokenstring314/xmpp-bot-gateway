"""OMEMO end-to-end test through a running AstrBot (built-in "!help" / "/help" command).

MODE=local  (default): local Prosody from tests/prosody/run.sh, accounts in tests/prosody/run/accounts.json
MODE=public: accounts from .test_bots.json, roles picked with BOT_JID / USER_JID

Checks:
  1. user sends an OMEMO-encrypted command -> the reply is OMEMO-encrypted, its plaintext body is only
     the fallback text, and it decrypts to the AstrBot help text
  2. user then sends the command in plaintext -> the reply is plaintext again
"""
import asyncio, json, os, sys
import slixmpp
from slixmpp.jid import JID
import tomli_w

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import run as run_gateway  # noqa: E402
from xmpp_gateway.xmpp import omemo as omemo_mod  # noqa: E402,F401  registers the xep_0384 plugin

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("xmpp_gateway").setLevel(logging.DEBUG)
MODE = os.environ.get("MODE", "local")
ASTRBOT_WS = os.environ.get("ASTRBOT_WS", "ws://127.0.0.1:6199/ws")
CMD = os.environ.get("CMD", "!help")
EXPECT = os.environ.get("EXPECT", "AstrBot")
WORK = f"data/e2e_omemo_{os.environ.get('MODE', 'local')}"  # OMEMO stores are kept between runs
results = {"encrypted_reply": None, "reply_not_leaked": None, "plain_reply_after_plain": None}
inbox: asyncio.Queue = asyncio.Queue()


class OmemoUser(slixmpp.ClientXMPP):
    def __init__(self, jid, pw, bot_jid):
        super().__init__(jid, pw)
        self.bot_jid = bot_jid
        self.use_message_ids = True
        self.register_plugin("xep_0380")
        self.register_plugin("xep_0384", {"store_path": f"{WORK}/user.json", "btbv": True}, module=omemo_mod)
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.on_msg)

    async def start(self, _):
        self.send_presence()
        await self.get_roster()
        await self["xep_0384"].get_session_manager()
        self.ready.set()

    async def on_msg(self, m):
        if m["type"] != "chat" or m["from"].bare != self.bot_jid:
            return
        if self["xep_0384"].is_encrypted(m):
            try:
                dec, _ = await self["xep_0384"].decrypt_message(m)
            except Exception as e:  # noqa: BLE001
                print("decrypt failed:", repr(e), flush=True)
                return
            if dec["body"]:
                await inbox.put(("enc", m["body"], dec["body"]))
        elif m["body"]:
            await inbox.put(("plain", m["body"], m["body"]))

    async def send_encrypted(self, text):
        msg = self.make_message(mto=self.bot_jid, mbody=text, mtype="chat")
        enc, errors = await self["xep_0384"].encrypt_message(msg, JID(self.bot_jid))
        if errors:
            print("encryption errors:", errors, flush=True)
        enc["eme"]["namespace"] = "eu.siacs.conversations.axolotl"
        enc["eme"]["name"] = "OMEMO"
        enc.send()


async def next_reply(want_kind, secs=45):
    """Next reply of the wanted kind that carries the expected text; other messages
    (e.g. late offline deliveries from earlier runs) are printed and skipped."""
    loop = asyncio.get_running_loop()
    end = loop.time() + secs
    while (left := end - loop.time()) > 0:
        try:
            r = await asyncio.wait_for(inbox.get(), left)
        except asyncio.TimeoutError:
            break
        if r[0] == want_kind and EXPECT in r[2]:
            return r
        print("skipped:", r[0], r[2][:50].replace("\n", " "), flush=True)
    return None


async def main():
    os.makedirs(WORK, exist_ok=True)
    local = MODE == "local"
    if local:
        acc = json.load(open("tests/prosody/run/accounts.json"))
        bot, user = acc["bot"], acc["user"]
        conn = {"server": "127.0.0.1", "port": 15222, "tls": "none"}
    else:
        accts = json.load(open(".test_bots.json"))
        pick = lambda env, i: next(a for a in accts if a["jid"] == os.environ[env]) if os.environ.get(env) else accts[i]
        bot, user, conn = pick("BOT_JID", 0), pick("USER_JID", 1), {}
    print(f"mode={MODE} bot={bot['jid']} user={user['jid']}", flush=True)
    cfg = {"xmpp": {**bot, **conn, "nick": "bot", "rooms": []},
           "omemo": {"enabled": True, "trust": "btbv", "store_dir": f"{WORK}/bot"},
           "storage": {"db": f"{WORK}/gateway.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [ASTRBOT_WS]}}
    path = f"{WORK}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())
    asyncio.create_task(run_gateway(path))
    await asyncio.sleep(10)

    u = OmemoUser(user["jid"], user["password"], bot["jid"])
    if local:
        u.enable_starttls = u.enable_direct_tls = False
        u.enable_plaintext = True
        u.plugin["feature_mechanisms"].unencrypted_plain = True
        u.connect(host="127.0.0.1", port=15222)
    else:
        u.connect()
    await asyncio.wait_for(u.ready.wait(), 40)
    u.send_presence_subscription(pto=bot["jid"])
    await asyncio.sleep(4)
    # PEP device lists are usually readable only by presence subscribers: fetch again after subscribing
    await u["xep_0384"].refresh_device_lists({JID(bot["jid"])}, force_download=True)

    await asyncio.sleep(5)
    while not inbox.empty():  # drop anything delivered from earlier runs (offline storage)
        print("stale:", inbox.get_nowait()[0], flush=True)
    await u.send_encrypted(CMD)
    r = await next_reply("enc")
    if r:
        _, outer, inner = r
        results["encrypted_reply"] = inner
        results["reply_not_leaked"] = "yes" if EXPECT not in outer else None
        print("encrypted reply, outer body:", outer[:60], flush=True)

    u.send_message(mto=bot["jid"], mbody=CMD, mtype="chat")
    r = await next_reply("plain")
    if r:
        results["plain_reply_after_plain"] = r[2]

    for k, v in results.items():
        print(f"[{k}]", "OK" if v else "FAIL", (v or "")[:100].replace("\n", " | "), flush=True)
    os._exit(0 if all(results.values()) else 1)

asyncio.run(main())
