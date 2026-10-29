"""End-to-end test against a real OneBot v11 bot framework.

Prerequisites: tests/prosody/run.sh is running, and the framework is running with a OneBot v11
adapter that accepts a reverse WebSocket at FRAMEWORK_WS. No LLM needed: a built-in or
test command (CMD, default "/help") must answer in a direct chat and in a group chat.
EXPECT (optional) is a substring the reply must contain. GROUP_PREFIX (optional) is put before
the command in the group, e.g. "bot: " to mention the bot.
"""
import asyncio, json, os, secrets, sys
import slixmpp
import tomli_w

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import run as run_gateway  # noqa: E402

FRAMEWORK_WS = os.environ.get("FRAMEWORK_WS") or os.environ.get("ASTRBOT_WS", "ws://127.0.0.1:6199/ws")
CMD = os.environ.get("CMD", "/help")
EXPECT = os.environ.get("EXPECT", "")
GROUP_PREFIX = os.environ.get("GROUP_PREFIX", "")  # e.g. "bot: " for frameworks that answer only when mentioned
replies = {"private": None, "group": None}


class TestUser(slixmpp.ClientXMPP):
    def __init__(self, jid, pw, bot_jid):
        super().__init__(jid, pw)
        self.bot_jid = bot_jid
        for x in ("xep_0030", "xep_0045", "xep_0004"): self.register_plugin(x)
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.on_msg)
        self.add_event_handler("groupchat_message", self.on_group)

    async def start(self, _):
        self.send_presence(); await self.get_roster()
        self.send_presence_subscription(pto=self.bot_jid)
        await asyncio.sleep(3)
        self.send_message(mto=self.bot_jid, mbody=CMD, mtype="chat")

    def on_msg(self, m):
        if m["type"] == "chat" and m["from"].bare == self.bot_jid and m["body"]:
            if EXPECT in m["body"]: replies["private"] = m["body"]

    def on_group(self, m):
        if m["mucnick"] == "bot" and m["body"] and not m.xml.find("{urn:xmpp:delay}delay") is not None:
            if EXPECT in m["body"]: replies["group"] = m["body"]


async def main():
    acc = json.load(open("tests/prosody/run/accounts.json"))
    room = f"fw-{secrets.token_hex(4)}@conference.test.local"
    cfg = {"xmpp": {"rate_limit": 0, **acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot", "rooms": [room]},
           "storage": {"db": "tests/prosody/run/gateway.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [FRAMEWORK_WS]}}
    path = "tests/prosody/run/e2e_framework.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())
    gw = asyncio.create_task(run_gateway(path))
    await asyncio.sleep(8)
    u = TestUser(acc["user"]["jid"], acc["user"]["password"], acc["bot"]["jid"])
    u.enable_starttls = u.enable_direct_tls = False; u.enable_plaintext = True
    u.plugin["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    await asyncio.sleep(8)
    u.plugin["xep_0045"].join_muc(room, "tester")
    await asyncio.sleep(3)
    u.send_message(mto=room, mbody=GROUP_PREFIX + CMD, mtype="groupchat")
    for _ in range(25):
        if all(replies.values()): break
        await asyncio.sleep(1)
    for k, v in replies.items():
        print(f"[{k}]", "OK" if v else "NO REPLY", (v or "")[:200].replace("\n", " | "), flush=True)
    os._exit(0 if all(replies.values()) else 1)

asyncio.run(main())
