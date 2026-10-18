"""Shared helpers for the end-to-end tests: a mock OneBot v11 app and a scripted XMPP user."""
import asyncio, json, time, uuid
import xml.etree.ElementTree as ET

import slixmpp
from aiohttp import web

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


