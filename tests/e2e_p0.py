"""P0 end-to-end test against a local Prosody and a mock OneBot v11 application.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_p0.py`.

Covers: lifecycle/heartbeat meta events, private + MUC message events, CQ-code and
array messages, @ mentions in both directions, get_* actions, delete_msg (XEP-0424),
OOB media links, error responses, reverse-WS reconnect, XMPP reconnect, a room that
fails to join, and starting with no rooms configured.
"""
import asyncio
import json
import logging
import os
import secrets
import sys
import time

import slixmpp
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6299
RUN = "tests/prosody/run"
results: dict[str, bool] = {}


def check(name: str, cond: bool, detail="") -> None:
    results[name] = bool(cond)
    print(f"{'PASS' if cond else 'FAIL'} {name}" + (f"  ({detail})" if detail and not cond else ""), flush=True)


async def until(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.2)
    return False


# ---------------- mock OneBot v11 application ----------------
class MockApp:
    def __init__(self):
        self.ws = None
        self.connections = 0
        self.self_id_header = None
        self.meta: list[dict] = []
        self.events: list[dict] = []
        self.pending: dict[str, asyncio.Future] = {}
        self.n = 0

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws, self.connections = ws, self.connections + 1
        self.self_id_header = request.headers.get("X-Self-ID")
        async for m in ws:
            d = json.loads(m.data)
            if "echo" in d and "post_type" not in d:
                fut = self.pending.pop(d["echo"], None)
                if fut and not fut.done():
                    fut.set_result(d)
            elif d.get("post_type") == "meta_event":
                self.meta.append(d | {"_conn": self.connections})
            elif d.get("post_type") == "message":
                self.events.append(d)
        return ws

    async def call(self, action, **params):
        self.n += 1
        echo = f"e{self.n}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}))
        return await asyncio.wait_for(fut, 10)

    def last_event(self, mtype):
        evs = [e for e in self.events if e["message_type"] == mtype]
        return evs[-1] if evs else None


# ---------------- test user ----------------
class TestUser(slixmpp.ClientXMPP):
    def __init__(self, jid, pw):
        super().__init__(jid, pw)
        for x in ("xep_0030", "xep_0045", "xep_0004", "xep_0066", "xep_0359"):
            self.register_plugin(x)
        self.inbox: list = []
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)
        self.add_event_handler("message", lambda m: self.inbox.append(m))
        self.add_event_handler("groupchat_message", lambda m: self.inbox.append(m))

    async def _start(self, _):
        self.send_presence()
        await self.get_roster()
        self.ready.set()

    def got(self, pred):
        return next((m for m in self.inbox if pred(m)), None)


def write_cfg(path, acc, rooms):
    cfg = {"xmpp": {**acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none",
                    "nick": "bot", "rooms": rooms},
           "storage": {"db": f"{RUN}/gateway_e2e.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"],
                          "heartbeat_interval": 1000, "reconnect_max": 2}}
    open(path, "wb").write(tomli_w.dumps(cfg).encode())


async def main():
    logging.basicConfig(level=logging.WARNING)
    acc = json.load(open(f"{RUN}/accounts.json"))
    room = f"p0-{secrets.token_hex(4)}@conference.test.local"
    cfg_path = f"{RUN}/e2e_p0.toml"
    write_cfg(cfg_path, acc, [room, "missing@conference.invalid"])

    app = MockApp()
    wapp = web.Application()
    wapp.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wapp)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    gw = Gateway(cfg_path)
    await gw.start()

    # --- connection + meta events ---
    await until(lambda: any(m["meta_event_type"] == "lifecycle" for m in app.meta))
    check("lifecycle connect", any(m.get("sub_type") == "connect" for m in app.meta))
    await until(lambda: gw.xmpp.online and room in gw.xmpp.rooms(), 20)
    check("joined room despite a failing room", room in gw.xmpp.rooms())
    await until(lambda: any(m["meta_event_type"] == "heartbeat" and m["status"]["online"] for m in app.meta))
    hb = [m for m in app.meta if m["meta_event_type"] == "heartbeat"]
    check("heartbeat with status", hb and hb[-1]["interval"] == 1000 and hb[-1]["status"]["online"])

    # --- basic actions & errors ---
    r = await app.call("get_login_info")
    self_id = r["data"]["user_id"]
    check("get_login_info matches X-Self-ID", str(self_id) == app.self_id_header, r)
    r = await app.call("get_version_info")
    check("get_version_info", r["retcode"] == 0 and r["data"]["protocol_version"] == "v11", r)
    r = await app.call("get_status")
    check("get_status online", r["data"]["online"] is True, r)
    r = await app.call("no_such_action")
    check("unsupported action -> 1404", r["status"] == "failed" and r["retcode"] == 1404, r)
    r = await app.call("send_private_msg", user_id=1, message="x")
    check("unknown user_id -> failed", r["status"] == "failed" and r["retcode"] == 100, r)
    r = await app.call("send_group_msg", group_id="abc", message="x")
    check("invalid group_id -> failed", r["status"] == "failed", r)

    # --- test user online ---
    user = TestUser(acc["user"]["jid"], acc["user"]["password"])
    user.enable_starttls = user.enable_direct_tls = False
    user.enable_plaintext = True
    user.plugin["feature_mechanisms"].unencrypted_plain = True
    user.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(user.ready.wait(), 15)
    bot = acc["bot"]["jid"]
    user.send_presence_subscription(pto=bot)
    await asyncio.sleep(2)

    # --- private message ---
    user.send_message(mto=bot, mbody="hello-private", mtype="chat")
    await until(lambda: app.last_event("private"))
    ev = app.last_event("private")
    check("private event", ev and ev["raw_message"] == "hello-private"
          and ev["message"] == [{"type": "text", "data": {"text": "hello-private"}}], ev)
    check("private sender nickname", ev and ev["sender"]["nickname"] == "user", ev and ev["sender"])
    uid = ev["user_id"]
    r = await app.call("send_private_msg", user_id=uid, message="pong &#91;ok&#93;")
    await until(lambda: user.got(lambda m: m["body"] == "pong [ok]"))
    check("send_private_msg with CQ escapes", user.got(lambda m: m["body"] == "pong [ok]") is not None)
    check("send returns message_id", r["retcode"] == 0 and r["data"]["message_id"] > 0, r)
    reply_mid = r["data"]["message_id"]

    r = await app.call("get_msg", message_id=ev["message_id"])
    check("get_msg incoming", r["retcode"] == 0 and r["data"]["raw_message"] == "hello-private", r)
    r = await app.call("get_friend_list")
    check("get_friend_list contains user", any(f["user_id"] == uid for f in r["data"] or []), r)
    r = await app.call("get_stranger_info", user_id=uid)
    check("get_stranger_info", r["retcode"] == 0 and r["data"]["nickname"] == "user", r)

    # --- delete_msg (XEP-0424) ---
    reply_stanza = user.got(lambda m: m["body"] == "pong [ok]")
    origin = reply_stanza.xml.find("{urn:xmpp:sid:0}origin-id")
    r = await app.call("delete_msg", message_id=reply_mid)
    await until(lambda: user.got(lambda m: m.xml.find("{urn:xmpp:message-retract:1}retract") is not None))
    rs = user.got(lambda m: m.xml.find("{urn:xmpp:message-retract:1}retract") is not None)
    check("delete_msg sends XEP-0424 retraction",
          r["retcode"] == 0 and rs is not None and origin is not None
          and rs.xml.find("{urn:xmpp:message-retract:1}retract").get("id") == origin.get("id"), r)
    r = await app.call("delete_msg", message_id=ev["message_id"])
    check("delete_msg on others' message refused", r["status"] == "failed", r)

    # --- media as OOB link ---
    url = "https://example.com/cat.png"
    await app.call("send_private_msg", user_id=uid, message=[{"type": "image", "data": {"file": url}}])
    await until(lambda: user.got(lambda m: m["body"] == url))
    om = user.got(lambda m: m["body"] == url)
    check("image url sent as body + OOB", om is not None and om["oob"]["url"] == url)
    r = await app.call("send_private_msg", user_id=uid, message=[{"type": "face", "data": {"id": "1"}}])
    check("empty rendered message -> failed", r["status"] == "failed", r)

    # --- group message with mention ---
    await user.plugin["xep_0045"].join_muc_wait(room, "tester", maxstanzas=0, timeout=15)
    user.send_message(mto=room, mbody="bot: hello-group", mtype="groupchat")
    await until(lambda: app.last_event("group"))
    gev = app.last_event("group")
    check("group event", gev and gev["message"][-1]["data"]["text"] == "hello-group", gev)
    check("group mention -> at self", gev and gev["message"][0] == {"type": "at", "data": {"qq": str(self_id)}}, gev)
    check("group sender card", gev and gev["sender"]["nickname"] == "tester", gev and gev["sender"])
    gid, guid = gev["group_id"], gev["user_id"]
    await app.call("send_group_msg", group_id=gid,
                   message=[{"type": "at", "data": {"qq": str(guid)}}, {"type": "text", "data": {"text": "pong-group"}}])
    await until(lambda: user.got(lambda m: m["type"] == "groupchat" and m["body"] == "tester: pong-group"))
    check("send_group_msg with at -> 'nick: '", user.got(lambda m: m["body"] == "tester: pong-group") is not None)
    r = await app.call("send_msg", message_type="group", group_id=gid, message="via send_msg")
    await until(lambda: user.got(lambda m: m["body"] == "via send_msg"))
    check("send_msg group", user.got(lambda m: m["body"] == "via send_msg") is not None, r)

    r = await app.call("get_group_list")
    check("get_group_list", any(g["group_id"] == gid for g in r["data"]), r)
    r = await app.call("get_group_info", group_id=gid)
    check("get_group_info", r["retcode"] == 0 and r["data"]["member_count"] == 2, r)
    r = await app.call("get_group_member_list", group_id=gid)
    check("get_group_member_list", sorted(m["nickname"] for m in r["data"] or []) == ["bot", "tester"], r)
    r = await app.call("get_group_member_info", group_id=gid, user_id=guid)
    check("get_group_member_info", r["retcode"] == 0 and r["data"]["nickname"] == "tester", r)

    # --- reverse WS reconnect ---
    conns = app.connections
    await app.ws.close()
    await until(lambda: app.connections > conns, 10)
    await until(lambda: any(m["_conn"] == app.connections and m["meta_event_type"] == "lifecycle" for m in app.meta), 5)
    check("reverse WS reconnects with lifecycle", app.connections > conns)
    r = await app.call("get_status")
    check("actions work after WS reconnect", r["retcode"] == 0, r)

    # --- XMPP reconnect ---
    gw.xmpp.transport.abort()
    await until(lambda: not gw.xmpp.online, 5)
    await until(lambda: gw.xmpp.online and room in gw.xmpp.rooms(), 30)
    await asyncio.sleep(1)
    n = len(app.events)
    user.send_message(mto=bot, mbody="after-reconnect", mtype="chat")
    user.send_message(mto=room, mbody="group-after-reconnect", mtype="groupchat")
    await until(lambda: len(app.events) >= n + 2, 15)
    got = {e["raw_message"] for e in app.events[n:]}
    check("XMPP reconnect: private + group resume", {"after-reconnect", "group-after-reconnect"} <= got, got)

    await gw.stop()

    # --- start with no rooms ---
    write_cfg(cfg_path, acc, [])
    app.meta.clear()
    gw2 = Gateway(cfg_path)
    await gw2.start()
    await until(lambda: gw2.xmpp.online, 15)
    await until(lambda: any(m["meta_event_type"] == "lifecycle" for m in app.meta), 10)
    n = len(app.events)
    user.send_message(mto=bot, mbody="no-rooms", mtype="chat")
    await until(lambda: len(app.events) > n, 10)
    r = await app.call("get_group_list")
    check("no rooms: starts, private works, empty group list",
          any(e["raw_message"] == "no-rooms" for e in app.events[n:]) and r["data"] == [], r)
    await gw2.stop()

    user.disconnect()
    await runner.cleanup()
    os.remove(cfg_path)
    failed = [k for k, v in results.items() if not v]
    print(f"RESULT {len(results) - len(failed)}/{len(results)} passed", "FAILED: " + ", ".join(failed) if failed else "")
    os._exit(1 if failed else 0)


asyncio.run(main())
