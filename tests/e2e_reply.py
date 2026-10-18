"""Replies (XEP-0461 + XEP-0428 fallback), delivery receipts (XEP-0184), mentions (XEP-0372)
and incoming retractions (XEP-0424 -> recall notices), end to end.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_reply.py`.
"""
import asyncio, json, logging, os, secrets, sys, time, uuid
import xml.etree.ElementTree as ET

import slixmpp
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6297
RUN = "tests/prosody/run"
WORK = "data/e2e_reply"
NS_REPLY, NS_FB, NS_RCPT = "urn:xmpp:reply:0", "urn:xmpp:fallback:0", "urn:xmpp:receipts"
NS_REF, NS_RETRACT = "urn:xmpp:reference:0", "urn:xmpp:message-retract:1"
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
        self.notices: list = []

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
            elif d.get("post_type") == "notice":
                self.notices.append(d)
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


def room_sid(m):
    for s in m.xml.findall("{urn:xmpp:sid:0}stanza-id"):
        if s.get("by") == m["from"].bare:
            return s.get("id")
    return None


async def main():
    logging.basicConfig(level=logging.WARNING)
    os.makedirs(WORK, exist_ok=True)
    acc = json.load(open(f"{RUN}/accounts.json"))
    bot_jid, user_jid = acc["bot"]["jid"], acc["user"]["jid"]
    room = f"reply-{secrets.token_hex(4)}@conference.test.local"
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
    gw = Gateway(path)
    await gw.start()
    u = User(user_jid, acc["user"]["password"])
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(u.ready.wait(), 20)
    await until(lambda: gw.xmpp.online and app.ws and room in gw.xmpp.rooms(), 20)

    # ---- private: bot replies to the user's message ----
    k = len(app.events)
    first = u.make_message(mto=bot_jid, mbody="what time is it", mtype="chat")
    first["id"] = uuid.uuid4().hex
    ET.SubElement(first.xml, f"{{{NS_RCPT}}}request")
    first.send()
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    uid, mid1 = ev["user_id"], ev["message_id"]
    rc = await until(lambda: first["id"] in u.receipts)
    check("receipt: bot acks a receipt request", rc, u.receipts)

    n = len(u.inbox)
    r = await app.call("send_private_msg", user_id=uid,
                       message=[{"type": "reply", "data": {"id": str(mid1)}}, {"type": "text", "data": {"text": "noon"}}])
    m = await until(lambda: u.got(lambda x: x["body"].endswith("noon"), n))
    rep = m.xml.find(f"{{{NS_REPLY}}}reply") if m else None
    fb = m.xml.find(f"{{{NS_FB}}}fallback/{{{NS_FB}}}body") if m else None
    check("private: outgoing reply references the user's message", rep is not None and rep.get("id") == first["id"], m)
    check("private: fallback quote covers exactly the quote",
          fb is not None and m["body"][:int(fb.get("end"))].startswith("> ")
          and m["body"][int(fb.get("end")):] == "noon", m["body"] if m else None)
    bot_msg_id, bot_mid = m["id"] if m else "", r["data"]["message_id"]

    # ---- private: user replies to the bot's message ----
    k = len(app.events)
    u.send_reply(bot_jid, "chat", "thanks", bot_jid, bot_msg_id, "> noon\n")
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    segs = ev["message"] if ev else []
    check("private: incoming reply -> reply segment with the bot's message_id",
          segs and segs[0] == {"type": "reply", "data": {"id": str(bot_mid)}}, segs)
    check("private: fallback quote stripped from text",
          len(segs) == 2 and segs[1] == {"type": "text", "data": {"text": "thanks"}}, segs)

    # ---- unknown reply target keeps the quote as text ----
    k = len(app.events)
    u.send_reply(bot_jid, "chat", "huh", bot_jid, "no-such-id", "> old\n")
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    check("private: unknown reply target keeps quote as text",
          ev and ev["message"] == [{"type": "text", "data": {"text": "> old\n"}}, {"type": "text", "data": {"text": "huh"}}],
          ev and ev["message"])

    # ---- group ----
    await u.plugin["xep_0045"].join_muc_wait(room, "tester", maxstanzas=0, timeout=15)
    k, n = len(app.events), len(u.inbox)
    u.send_message(mto=room, mbody="group question", mtype="groupchat")
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    own = await until(lambda: u.got(lambda x: x["type"] == "groupchat" and x["body"] == "group question", n))
    gid, gmid = ev["group_id"], ev["message_id"]
    n = len(u.inbox)
    r = await app.call("send_group_msg", group_id=gid,
                       message=[{"type": "reply", "data": {"id": str(gmid)}}, {"type": "text", "data": {"text": "group answer"}}])
    m = await until(lambda: u.got(lambda x: x["type"] == "groupchat" and x["body"].endswith("group answer"), n))
    rep = m.xml.find(f"{{{NS_REPLY}}}reply") if m else None
    check("group: outgoing reply uses the room's stanza-id and occupant JID",
          rep is not None and rep.get("id") == room_sid(own) and rep.get("to") == f"{room}/tester",
          (rep.attrib if rep is not None else None, room_sid(own) if own else None))
    gbot_mid = r["data"]["message_id"]
    await asyncio.sleep(1)
    k = len(app.events)
    u.send_reply(room, "groupchat", "bot: nice", f"{room}/bot", room_sid(m), "> group answer\n")
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    segs = ev["message"] if ev else []
    check("group: reply to the bot's message -> reply segment with its message_id",
          segs and segs[0] == {"type": "reply", "data": {"id": str(gbot_mid)}}, segs)
    check("group: mention after the quote still becomes an at segment",
          len(segs) == 3 and segs[1]["type"] == "at" and segs[2] == {"type": "text", "data": {"text": "nice"}}, segs)

    # ---- XEP-0372 mentions ----
    k = len(app.events)
    mm = u.make_message(mto=room, mbody="hey bot, look", mtype="groupchat")
    ET.SubElement(mm.xml, f"{{{NS_REF}}}reference", {"type": "mention", "begin": "4", "end": "7", "uri": f"xmpp:{room}/bot"})
    mm.send()
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    check("group: XEP-0372 mention of the bot -> at segment",
          ev and ev["message"][0]["type"] == "at" and ev["message"][-1]["data"].get("text") == "hey bot, look",
          ev and ev["message"])
    n = len(u.inbox)
    await app.call("send_group_msg", group_id=gid,
                   message=[{"type": "at", "data": {"qq": str(ev["user_id"])}}, {"type": "text", "data": {"text": "hi"}}])
    m = await until(lambda: u.got(lambda x: x["type"] == "groupchat" and x["body"] == "tester: hi", n))
    ref = m.xml.find(f"{{{NS_REF}}}reference") if m else None
    check("group: outgoing at -> XEP-0372 reference",
          ref is not None and (ref.get("begin"), ref.get("end"), ref.get("uri")) == ("0", "6", f"xmpp:{room}/tester"),
          ref.attrib if ref is not None else m)

    # ---- incoming retractions -> recall notices ----
    k = len(app.events)
    oops = u.make_message(mto=bot_jid, mbody="oops", mtype="chat")
    oops["id"] = uuid.uuid4().hex
    oops.send()
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    j = len(app.notices)
    rt = u.make_message(mto=bot_jid, mtype="chat", mbody="retracted")
    ET.SubElement(rt.xml, f"{{{NS_RETRACT}}}retract", {"id": oops["id"]})
    rt.send()
    nt = await until(lambda: len(app.notices) > j and app.notices[-1])
    check("private: retraction -> friend_recall notice",
          nt and nt["notice_type"] == "friend_recall" and nt["message_id"] == ev["message_id"]
          and nt["user_id"] == ev["user_id"], nt)
    j = len(app.notices)
    rt = u.make_message(mto=bot_jid, mtype="chat")
    ET.SubElement(rt.xml, f"{{{NS_RETRACT}}}retract", {"id": bot_msg_id})
    rt.send()
    await asyncio.sleep(2)
    check("private: retracting someone else's message is ignored", len(app.notices) == j, app.notices[j:])
    k, n = len(app.events), len(u.inbox)
    u.send_message(mto=room, mbody="group oops", mtype="groupchat")
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    own = await until(lambda: u.got(lambda x: x["type"] == "groupchat" and x["body"] == "group oops", n))
    j = len(app.notices)
    rt = u.make_message(mto=room, mtype="groupchat", mbody="retracted")
    ET.SubElement(rt.xml, f"{{{NS_RETRACT}}}retract", {"id": room_sid(own)})
    rt.send()
    nt = await until(lambda: len(app.notices) > j and app.notices[-1])
    check("group: retraction -> group_recall notice",
          nt and nt["notice_type"] == "group_recall" and nt["message_id"] == ev["message_id"]
          and nt["group_id"] == ev["group_id"], nt)

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


asyncio.run(main())
