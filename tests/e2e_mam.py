"""MAM catch-up (XEP-0313): messages sent while the gateway was offline are delivered after it comes back.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_mam.py`.
"""
import asyncio, json, logging, os, secrets, sys, time, uuid
import xml.etree.ElementTree as ET

import slixmpp
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6298
RUN = "tests/prosody/run"
WORK = "data/e2e_mam"
NS_REPLY, NS_FB, NS_RCPT = "urn:xmpp:reply:0", "urn:xmpp:fallback:0", "urn:xmpp:receipts"
results: dict[str, bool] = {}


def check(name, cond, detail=""):
    results[name] = bool(cond)
    print(f"{'PASS' if cond else 'FAIL'} {name}" + (f"  ({detail})" if detail and not cond else ""), flush=True)


async def until(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        r = pred()
        if r:
            return r
        await asyncio.sleep(0.2)
    return None


class MockApp:
    def __init__(self):
        self.ws, self.events, self.pending, self.n = None, [], {}, 0

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        async for m in ws:
            d = json.loads(m.data)
            if "echo" in d and "post_type" not in d:
                fut = self.pending.pop(d["echo"], None)
                if fut and not fut.done():
                    fut.set_result(d)
            elif d.get("post_type") == "message":
                self.events.append(d)
        return ws

    async def call(self, action, **params):
        self.n += 1
        echo = f"e{self.n}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}))
        return await asyncio.wait_for(fut, 20)


class User(slixmpp.ClientXMPP):
    def __init__(self, jid, pw):
        super().__init__(jid, pw)
        for x in ("xep_0030", "xep_0045", "xep_0004", "xep_0359"):
            self.register_plugin(x)
        self.register_plugin("xep_0184", {"auto_ack": False, "auto_request": False})
        self.receipts: list[str] = []
        self.add_event_handler("receipt_received", lambda m: self.receipts.append(m["receipt"]))
        self.inbox: list = []
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)
        self.add_event_handler("message", lambda m: self.inbox.append(m))
        self.add_event_handler("groupchat_message", lambda m: self.inbox.append(m))

    async def _start(self, _):
        self.send_presence()
        await self.get_roster()
        self.ready.set()

    def got(self, pred, start=0):
        return next((m for m in self.inbox[start:] if pred(m)), None)

    def send_reply(self, to, mtype, body, reply_to, reply_id, quote, request_receipt=False):
        msg = self.make_message(mto=to, mbody=quote + body, mtype=mtype)
        msg["id"] = uuid.uuid4().hex
        ET.SubElement(msg.xml, f"{{{NS_REPLY}}}reply", {"to": reply_to, "id": reply_id})
        fb = ET.SubElement(msg.xml, f"{{{NS_FB}}}fallback", {"for": NS_REPLY})
        ET.SubElement(fb, f"{{{NS_FB}}}body", {"start": "0", "end": str(len(quote))})
        if request_receipt:
            ET.SubElement(msg.xml, f"{{{NS_RCPT}}}request")
        msg.send()
        return msg["id"]



def texts(evs):
    return [e["raw_message"] for e in evs]


async def main():
    logging.basicConfig(level=logging.WARNING)
    os.makedirs(WORK, exist_ok=True)
    acc = json.load(open(f"{RUN}/accounts.json"))
    bot_jid, user_jid = acc["bot"]["jid"], acc["user"]["jid"]
    room = f"mam-{secrets.token_hex(4)}@conference.test.local"
    db = f"{WORK}/gateway.db"
    if os.path.exists(db):
        os.unlink(db)
    cfg = {"xmpp": {"rate_limit": 0, **acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot", "rooms": [room]},
           "storage": {"db": db},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())

    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wa)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    u = User(user_jid, acc["user"]["password"])
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(u.ready.wait(), 20)

    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws and room in gw.xmpp.rooms(), 20)
    await asyncio.sleep(3)
    check("first run: old archive is not replayed", len(app.events) == 0, texts(app.events))

    await u.plugin["xep_0045"].join_muc_wait(room, "tester", maxstanzas=0, timeout=15)
    u.send_message(mto=bot_jid, mbody="live-private", mtype="chat")
    u.send_message(mto=room, mbody="live-group", mtype="groupchat")
    await until(lambda: len(app.events) >= 2)
    check("live messages delivered", sorted(texts(app.events)) == ["live-group", "live-private"], texts(app.events))

    await gw.stop()
    await asyncio.sleep(2)
    t_off = time.time()
    for i in (1, 2):
        u.send_message(mto=bot_jid, mbody=f"missed-private-{i}", mtype="chat")
        u.send_message(mto=room, mbody=f"missed-group-{i}", mtype="groupchat")
        await asyncio.sleep(0.3)
    await asyncio.sleep(1)

    k = len(app.events)
    gw = Gateway(path)
    await gw.start()
    await until(lambda: len(app.events) >= k + 4, 25)
    await asyncio.sleep(3)   # give duplicates a chance to show up
    new = app.events[k:]
    check("missed private messages caught up in order",
          [t for t in texts(new) if "private" in t] == ["missed-private-1", "missed-private-2"], texts(new))
    check("missed group messages caught up in order",
          [t for t in texts(new) if "group" in t] == ["missed-group-1", "missed-group-2"], texts(new))
    check("no duplicates and no replay of already-delivered messages", len(new) == 4, texts(new))
    check("caught-up events keep their original time",
          all(t_off - 2 <= e["time"] <= t_off + 5 for e in new), [e["time"] for e in new])
    grp = next((e for e in new if e["raw_message"] == "missed-group-1"), None)
    check("caught-up group event has group_id", grp and grp.get("message_type") == "group" and grp.get("group_id"), grp)

    # live traffic still works and a reply to a caught-up message resolves
    n = len(u.inbox)
    pm = next((e for e in new if e["raw_message"] == "missed-private-2"), None)
    r = await app.call("send_private_msg", user_id=pm["user_id"],
                       message=[{"type": "reply", "data": {"id": str(pm["message_id"])}}, {"type": "text", "data": {"text": "got it"}}])
    m = await until(lambda: u.got(lambda x: x["body"].endswith("got it"), n))
    check("reply to a caught-up message carries a reply reference",
          r["status"] == "ok" and m is not None and m.xml.find(f"{{{NS_REPLY}}}reply") is not None, r)

    # restart again without new traffic: nothing is replayed
    await gw.stop()
    await asyncio.sleep(1)
    k = len(app.events)
    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and room in gw.xmpp.rooms(), 20)
    await asyncio.sleep(4)
    check("second restart replays nothing", len(app.events) == k, texts(app.events[k:]))

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


asyncio.run(main())
