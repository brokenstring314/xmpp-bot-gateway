"""与真实 AstrBot 的端到端测试。

前置：tests/prosody/run.sh 已启动；AstrBot 已运行，并配置了 OneBot v11 (aiocqhttp) 适配器，
反向 WS 监听 127.0.0.1:6199（路径 /ws）。不需要大模型：用 AstrBot 自带的 /help 指令验证。
"""
import asyncio, json, os, secrets, sys
import slixmpp
import tomli_w

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import run as run_gateway  # noqa: E402

ASTRBOT_WS = os.environ.get("ASTRBOT_WS", "ws://127.0.0.1:6199/ws")
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
        self.send_message(mto=self.bot_jid, mbody="/help", mtype="chat")

    def on_msg(self, m):
        if m["type"] == "chat" and m["from"].bare == self.bot_jid and m["body"]:
            replies["private"] = m["body"]

    def on_group(self, m):
        if m["mucnick"] == "bot" and m["body"] and not m.xml.find("{urn:xmpp:delay}delay") is not None:
            replies["group"] = m["body"]


async def main():
    acc = json.load(open("tests/prosody/run/accounts.json"))
    room = f"astrbot-{secrets.token_hex(4)}@conference.test.local"
    cfg = {"xmpp": {**acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot", "rooms": [room]},
           "storage": {"db": "tests/prosody/run/gateway.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [ASTRBOT_WS]}}
    path = "tests/prosody/run/e2e_astrbot.toml"
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
    u.send_message(mto=room, mbody="/help", mtype="groupchat")
    for _ in range(25):
        if all(replies.values()): break
        await asyncio.sleep(1)
    for k, v in replies.items():
        print(f"[{k}]", "OK" if v else "NO REPLY", (v or "")[:200].replace("\n", " | "), flush=True)
    os._exit(0 if all(replies.values()) else 1)

asyncio.run(main())
