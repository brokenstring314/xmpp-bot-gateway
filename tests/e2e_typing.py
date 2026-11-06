"""Chat state notifications (XEP-0085), end to end: the bot shows "typing" while the framework handles a
direct message, clears it with the reply or after `typing_timeout`; contacts' typing reported as
notice.notify.input_status; set_input_status; nothing sent to contacts that never sent chat states.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_typing.py`.
"""
import asyncio, logging, os, secrets, subprocess, sys
import xml.etree.ElementTree as ET

import slixmpp
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from harness import MockApp, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6302
RUN = "tests/prosody/run"
WORK = "data/e2e_typing"
CS = "http://jabber.org/protocol/chatstates"


def register(name):
    pw = secrets.token_urlsafe(12)
    cfg = f"{RUN}/prosody.cfg.lua"
    subprocess.run(["prosodyctl", "--config", cfg, "deluser", f"{name}@test.local"], capture_output=True)
    subprocess.run(["prosodyctl", "--config", cfg, "register", name, "test.local", pw], check=True,
                   capture_output=True)
    return f"{name}@test.local", pw


def state_of(m):
    el = next((c for c in m.xml if c.tag.startswith(f"{{{CS}}}")), None)
    return el.tag.split("}")[1] if el is not None else None


class Peer(slixmpp.ClientXMPP):
    def __init__(self, jid, pw):
        super().__init__(jid, pw)
        self.inbox: list = []
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)
        self.register_plugin("xep_0085")
        self.add_event_handler("message", lambda m: self.inbox.append(m))
        # slixmpp only fires "message" for stanzas with a body; bodyless chat states come as "chatstate"
        self.add_event_handler("chatstate", lambda m: None if m["body"] else self.inbox.append(m))
        self.enable_starttls = self.enable_direct_tls = False
        self.enable_plaintext = True
        self.plugin["feature_mechanisms"].unencrypted_plain = True

    async def _start(self, _):
        self.send_presence()
        await self.get_roster()
        self.ready.set()

    def say(self, to, body=None, state=None):
        m = self.make_message(mto=to, mbody=body, mtype="chat")
        if state:
            ET.SubElement(m.xml, f"{{{CS}}}{state}")
        m.send()

    def states(self, start=0):
        return [(state_of(m), m["body"]) for m in self.inbox[start:] if m["type"] == "chat"]


async def peer(name):
    jid, pw = register(name)
    p = Peer(jid, pw)
    p.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(p.ready.wait(), 20)
    return p


def write_cfg(bot, name, db, **xmpp):
    cfg = {"xmpp": {"jid": bot[0], "password": bot[1], "server": "127.0.0.1", "port": 15222, "tls": "none",
                    "rate_limit": 0, "mam_catchup": False, **xmpp},
           "storage": {"db": db},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/{name}.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())
    return path


async def start_gw(path, app):
    app.ws = None
    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws is not None, 20)
    return gw


async def main():
    logging.basicConfig(level=logging.WARNING)
    os.makedirs(WORK, exist_ok=True)
    t = secrets.token_hex(3)
    bot = register(f"tbot{t}")
    db = f"{WORK}/gateway.db"
    if os.path.exists(db):
        os.unlink(db)
    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wa)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    gw = await start_gw(write_cfg(bot, "on", db, typing_timeout=3, report_typing=True), app)
    ann = await peer(f"tann{t}")     # sends chat states, like Conversations / Gajim
    ben = await peer(f"tben{t}")     # never sends chat states
    a_id = await gw.idmap.to_int("user", ann.boundjid.bare)
    b_id = await gw.idmap.to_int("user", ben.boundjid.bare)

    # ---- bot typing while the framework handles a message ----
    k, i = len(app.events), len(ann.inbox)
    ann.say(bot[0], "hello", "active")
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    check("message with chat state reaches the framework", ev and ev["raw_message"] == "hello", app.events[k:])
    check("bot shows composing after the message is delivered",
          await until(lambda: ("composing", "") in ann.states(i)), ann.states(i))
    r = await app.call("send_private_msg", user_id=a_id, message="hi ann")
    check("reply sent", r["status"] == "ok", r)
    check("reply carries <active/>", await until(lambda: ("active", "hi ann") in ann.states(i)), ann.states(i))
    await asyncio.sleep(4)
    check("no timeout <active/> after the reply cleared it",
          [s for s in ann.states(i) if s == ("active", "")] == [], ann.states(i))

    i = len(ann.inbox)
    ann.say(bot[0], "no reply to this one", "active")
    check("composing again for the next message", await until(lambda: ("composing", "") in ann.states(i)),
          ann.states(i))
    check("typing cleared after typing_timeout without a reply",
          await until(lambda: ("active", "") in ann.states(i), 8), ann.states(i))

    # ---- contacts without chat-state support get none ----
    k, j = len(app.events), len(ben.inbox)
    ben.say(bot[0], "plain hello")
    await until(lambda: len(app.events) > k)
    r = await app.call("send_private_msg", user_id=b_id, message="hi ben")
    await until(lambda: any(m["body"] == "hi ben" for m in ben.inbox[j:]))
    check("no chat states to a contact that never sent any",
          all(s is None for s, _ in ben.states(j)) and ben.states(j), ben.states(j))
    r = await app.call("set_input_status", user_id=b_id, event_type=1)
    check("set_input_status to such a contact sends nothing",
          r["status"] == "ok" and r["data"]["sent"] is False, r)

    # ---- set_input_status ----
    i = len(ann.inbox)
    r = await app.call("set_input_status", user_id=a_id, event_type=1)
    check("set_input_status 1 -> composing",
          r["status"] == "ok" and r["data"]["sent"] and await until(lambda: ("composing", "") in ann.states(i)),
          (r, ann.states(i)))
    r = await app.call("set_input_status", user_id=a_id, event_type=0)
    check("set_input_status 0 -> active", await until(lambda: ("active", "") in ann.states(i)), ann.states(i))
    r = await app.call("set_input_status", user_id=a_id, event_type="x")
    check("set_input_status with a bad event_type fails", r["status"] == "failed", r)

    # ---- contact typing -> notice.notify.input_status ----
    def typing_notices(n):
        return [x for x in app.notices[n:] if x.get("sub_type") == "input_status"]
    n = len(app.notices)
    ann.say(bot[0], state="composing")
    nt = await until(lambda: typing_notices(n))
    check("contact composing -> input_status event_type 1",
          nt and nt[0]["notice_type"] == "notify" and nt[0]["event_type"] == 1 and nt[0]["user_id"] == a_id, nt)
    ann.say(bot[0], state="composing")
    await asyncio.sleep(1.5)
    check("repeated composing is not reported twice", len(typing_notices(n)) == 1, typing_notices(n))
    ann.say(bot[0], state="paused")
    check("contact paused -> input_status event_type 0",
          await until(lambda: len(typing_notices(n)) == 2 and typing_notices(n)[1]["event_type"] == 0),
          typing_notices(n))
    ann.say(bot[0], state="active")
    await asyncio.sleep(1.5)
    check("active after paused is not reported again", len(typing_notices(n)) == 2, typing_notices(n))
    await gw.stop()

    # ---- chat_states = false, report_typing = false (default) ----
    gw = await start_gw(write_cfg(bot, "off", db, chat_states=False), app)
    k, i, n = len(app.events), len(ann.inbox), len(app.notices)
    ann.say(bot[0], state="composing")
    ann.say(bot[0], "hello again", "active")
    await until(lambda: len(app.events) > k)
    r = await app.call("send_private_msg", user_id=a_id, message="hi again")
    await until(lambda: any(m["body"] == "hi again" for m in ann.inbox[i:]))
    await asyncio.sleep(1)
    check("chat_states off: no chat states sent", all(s is None for s, _ in ann.states(i)), ann.states(i))
    check("report_typing off by default: no input_status", typing_notices(n) == [], typing_notices(n))
    await gw.stop()

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
