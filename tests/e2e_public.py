"""Public-network end-to-end test against real XMPP providers and a real AstrBot.

Accounts come from .test_bots.json (gitignored): a list of {"jid", "password"}.
BOT_JID / USER_JID env vars pick which entries play bot and user (different
servers exercise federation). AstrBot must already be listening on ASTRBOT_WS.

Steps: user subscribes to bot -> private "!help" -> user creates a MUC on its
own server -> direct-invites the bot (tests auto-join) -> group "!help".
"""
import asyncio, json, os, secrets, sys
import slixmpp
import tomli_w

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import run as run_gateway  # noqa: E402

ASTRBOT_WS = os.environ.get("ASTRBOT_WS", "ws://127.0.0.1:6199/ws")
CMD = os.environ.get("CMD", "!help")
NICK = "bot"
results = {"private": None, "invite_join": None, "group": None}


class TestUser(slixmpp.ClientXMPP):
    def __init__(self, jid, pw, bot_jid):
        super().__init__(jid, pw)
        self.bot_jid = bot_jid
        for x in ("xep_0030", "xep_0004", "xep_0045", "xep_0249"):
            self.register_plugin(x)
        self.room = None
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.on_msg)
        self.add_event_handler("groupchat_message", self.on_group)
        self.add_event_handler("groupchat_presence", self.on_gpres)

    async def start(self, _):
        self.send_presence()
        await self.get_roster()
        self.ready.set()

    async def muc_service(self):
        items = await self["xep_0030"].get_items(jid=self.boundjid.domain)
        for it in items["disco_items"]["items"]:
            try:
                info = await self["xep_0030"].get_info(jid=it[0])
                if "http://jabber.org/protocol/muc" in info["disco_info"]["features"]:
                    return it[0]
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError("no MUC service on " + self.boundjid.domain)

    async def create_room(self):
        self.room = f"gwtest-{secrets.token_hex(4)}@{await self.muc_service()}"
        self["xep_0045"].join_muc(self.room, "tester")
        await asyncio.sleep(3)
        iq = self.Iq(stype="set", sto=self.room)
        q = iq.xml.makeelement("{http://jabber.org/protocol/muc#owner}query", {})
        q.append(q.makeelement("{jabber:x:data}x", {"type": "submit"}))  # instant room
        iq.xml.append(q)
        try:
            await iq.send(timeout=10)
        except Exception as e:  # noqa: BLE001  room may already be unlocked
            print("room config:", repr(e), flush=True)
        return self.room

    def on_msg(self, m):
        if m["type"] == "chat" and m["from"].bare == self.bot_jid and m["body"]:
            results["private"] = m["body"]

    def on_gpres(self, p):
        if self.room and p["from"].bare == self.room and p["from"].resource == NICK and p["type"] != "unavailable":
            results["invite_join"] = "joined"

    def on_group(self, m):
        if m["mucnick"] == NICK and m["body"] and m.xml.find("{urn:xmpp:delay}delay") is None:
            results["group"] = m["body"]


async def wait_for(key, secs):
    for _ in range(secs):
        if results[key]:
            return True
        await asyncio.sleep(1)
    return False


def pick(accts, env, default_idx):
    want = os.environ.get(env)
    return next(a for a in accts if a["jid"] == want) if want else accts[default_idx]


async def main():
    accts = json.load(open(".test_bots.json"))
    bot, user = pick(accts, "BOT_JID", 0), pick(accts, "USER_JID", 1)
    print(f"bot={bot['jid']} user={user['jid']}", flush=True)
    os.makedirs("data", exist_ok=True)
    cfg = {"xmpp": {"rate_limit": 0, **bot, "nick": NICK, "rooms": [], "accept_invites": "all"},
           "storage": {"db": "data/e2e_public.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [ASTRBOT_WS]}}
    path = "data/e2e_public.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())
    asyncio.create_task(run_gateway(path))
    await asyncio.sleep(8)

    u = TestUser(user["jid"], user["password"], bot["jid"])
    u.connect()
    await asyncio.wait_for(u.ready.wait(), 30)
    u.send_presence_subscription(pto=bot["jid"])
    await asyncio.sleep(4)
    u.send_message(mto=bot["jid"], mbody=CMD, mtype="chat")
    await wait_for("private", 25)

    room = await u.create_room()
    print("room:", room, flush=True)
    u["xep_0249"].send_invitation(bot["jid"], room, reason="e2e")
    if await wait_for("invite_join", 25):
        await asyncio.sleep(2)
        u.send_message(mto=room, mbody=CMD, mtype="groupchat")
        await wait_for("group", 25)

    for k, v in results.items():
        print(f"[{k}]", "OK" if v else "FAIL", (v or "")[:160].replace("\n", " | "), flush=True)
    try:
        u["xep_0045"].leave_muc(room, "tester")
        await asyncio.sleep(1)
    except Exception:  # noqa: BLE001
        pass
    os._exit(0 if all(results.values()) else 1)

asyncio.run(main())
