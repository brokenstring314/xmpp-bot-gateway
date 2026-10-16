"""P0 端到端测试：模拟 OneBot v11 应用（echo bot）+ 测试用户账号。

用法：先 tests/prosody/run.sh 启动本地 Prosody，再 python tests/e2e_p0.py
流程：启动 mock 反向 WS 服务 -> 启动网关 -> 测试用户给 bot 发私聊 / 群聊
     -> mock 收到 OneBot 事件后调用 send_private_msg / send_group_msg 回复 -> 测试用户收到回复即通过。
"""
import asyncio, json, secrets, sys, tomllib, logging
from aiohttp import web
import slixmpp

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import run as run_gateway  # noqa: E402

PORT = 6199
results = {"private": False, "group": False}
events_seen = []


async def mock_onebot(request):
    ws = web.WebSocketResponse(); await ws.prepare(request)
    print("[mock] gateway connected, X-Self-ID =", request.headers.get("X-Self-ID"))
    async for m in ws:
        d = json.loads(m.data)
        if d.get("post_type") != "message":
            continue
        events_seen.append(d)
        print("[mock] event:", d["message_type"], d.get("raw_message"))
        if d["message_type"] == "private":
            act = {"action": "send_private_msg", "params": {"user_id": d["user_id"], "message": "pong: " + d["raw_message"]}, "echo": "1"}
        else:
            act = {"action": "send_group_msg", "params": {"group_id": d["group_id"], "message": [{"type": "text", "data": {"text": "pong: " + d["raw_message"]}}]}, "echo": "2"}
        await ws.send_str(json.dumps(act))
    return ws


class TestUser(slixmpp.ClientXMPP):
    def __init__(self, jid, pw, bot_jid, room):
        super().__init__(jid, pw)
        self.bot_jid, self.room = bot_jid, room
        for x in ("xep_0030", "xep_0045", "xep_0004"): self.register_plugin(x)
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.on_msg)
        self.add_event_handler("groupchat_message", self.on_group)

    async def start(self, _):
        self.send_presence(); await self.get_roster()
        # 服务器可能拦截陌生人消息：先互加好友（网关默认自动同意并回加）
        self.send_presence_subscription(pto=self.bot_jid)
        await asyncio.sleep(5)
        self.send_message(mto=self.bot_jid, mbody="hello-private", mtype="chat")

    def on_msg(self, m):
        if m["type"] == "chat" and m["body"].startswith("pong: hello-private"):
            print("[user] got private reply:", m["body"]); results["private"] = True

    def on_group(self, m):
        if m["body"].startswith("pong: hello-group"):
            print("[user] got group reply:", m["body"]); results["group"] = True


async def main():
    logging.basicConfig(level=logging.WARNING)
    acc = json.load(open("tests/prosody/run/accounts.json"))
    cfg = {"xmpp": {**acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot"},
           "storage": {"db": "tests/prosody/run/gateway.db"}, "onebot_v11": {"enabled": True}}
    u = acc["user"]
    room = f"p0test-{secrets.token_hex(4)}@conference.test.local"

    # mock app
    app = web.Application(); app.router.add_get("/ws", mock_onebot)
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, "127.0.0.1", PORT).start()

    # 网关配置：反向 WS 指向 mock，加入测试房间
    cfg["onebot_v11"]["reverse_ws"] = [f"ws://127.0.0.1:{PORT}/ws"]
    cfg["xmpp"]["rooms"] = [room]
    import tomli_w
    open("data_e2e_config.toml", "wb").write(tomli_w.dumps(cfg).encode())

    user = TestUser(u["jid"], u["password"], cfg["xmpp"]["jid"], room)
    gw = asyncio.create_task(run_gateway("data_e2e_config.toml"))
    await asyncio.sleep(8)  # 等 bot 上线并建好房间
    user.enable_starttls = user.enable_direct_tls = False; user.enable_plaintext = True
    user.plugin["feature_mechanisms"].unencrypted_plain = True
    user.connect(host="127.0.0.1", port=15222)
    await asyncio.sleep(10)
    # 群聊：测试用户加入同一房间并发言
    user.plugin["xep_0045"].join_muc(room, "tester")
    await asyncio.sleep(4)
    user.send_message(mto=room, mbody="hello-group", mtype="groupchat")
    for _ in range(20):
        if all(results.values()): break
        await asyncio.sleep(1)
    print("RESULT", json.dumps(results), "events:", len(events_seen))
    if events_seen:
        print("sample event:", json.dumps(events_seen[0], ensure_ascii=False))
    gw.cancel(); user.disconnect(); await runner.cleanup()
    import os; os.remove("data_e2e_config.toml")
    os._exit(0 if all(results.values()) else 1)

asyncio.run(main())
