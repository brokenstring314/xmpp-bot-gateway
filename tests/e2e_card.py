"""Bot nickname and room name, end to end: set_group_card (the bot's own nickname, kept across restarts) and
set_group_name (room name, owner only).

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_card.py`.
"""
import asyncio, logging, os, secrets, subprocess, sys

import slixmpp
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from harness import MockApp, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6304
RUN = "tests/prosody/run"
WORK = "data/e2e_card"


def register(name):
    pw = secrets.token_urlsafe(12)
    cfg = f"{RUN}/prosody.cfg.lua"
    subprocess.run(["prosodyctl", "--config", cfg, "deluser", f"{name}@test.local"], capture_output=True)
    subprocess.run(["prosodyctl", "--config", cfg, "register", name, "test.local", pw], check=True,
                   capture_output=True)
    return f"{name}@test.local", pw


class Peer(slixmpp.ClientXMPP):
    def __init__(self, jid, pw):
        super().__init__(jid, pw)
        self.ready = asyncio.Event()
        for p in ("xep_0030", "xep_0004", "xep_0045"):
            self.register_plugin(p)
        self.add_event_handler("session_start", self._start)
        self.enable_starttls = self.enable_direct_tls = False
        self.enable_plaintext = True
        self.plugin["feature_mechanisms"].unencrypted_plain = True

    async def _start(self, _):
        self.send_presence()
        await self.get_roster()
        self.ready.set()

    async def join(self, room, nick):
        pres = await self.plugin["xep_0045"].join_muc_wait(slixmpp.JID(room), nick, maxstanzas=0, timeout=15)
        pres = pres[0] if isinstance(pres, tuple) else pres
        if 201 in pres["muc"]["status_codes"]:
            form = self.plugin["xep_0004"].make_form(ftype="submit")
            await self.plugin["xep_0045"].set_room_config(room, form)

    def on_room(self, room, cb):
        self.add_event_handler(f"muc::{room}::presence", cb)
        self.add_event_handler(f"muc::{room}::message", cb)

    def leave(self, room, nick):
        self.plugin["xep_0045"].leave_muc(slixmpp.JID(room), nick)


async def peer(name):
    jid, pw = register(name)
    p = Peer(jid, pw)
    p.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(p.ready.wait(), 20)
    return p


def write_cfg(bot, name, db, room, **xmpp):
    cfg = {"xmpp": {"jid": bot[0], "password": bot[1], "server": "127.0.0.1", "port": 15222, "tls": "none",
                    "rate_limit": 0, "mam_catchup": False, "rooms": [room], "nick": "gwbot", **xmpp},
           "storage": {"db": db},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/{name}.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())
    return path


async def start_gw(path, app, room):
    app.ws = None
    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws is not None and room in gw.xmpp._room_ready, 20)
    return gw


async def main():
    logging.basicConfig(level=logging.WARNING)
    os.makedirs(WORK, exist_ok=True)
    t = secrets.token_hex(3)
    room = f"card{t}@conference.test.local"
    bot = register(f"cbot{t}")
    db = f"{WORK}/gateway.db"
    if os.path.exists(db):
        os.unlink(db)
    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wa)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    owner = await peer(f"cown{t}")
    await owner.join(room, "owner")
    seen: list = []
    owner.on_room(room, seen.append)
    cfg = write_cfg(bot, "gw", db, room)
    gw = await start_gw(cfg, app, room)
    gid = await gw.idmap.to_int("group", room)
    r = await app.call("get_login_info")
    me = r["data"]["user_id"]
    occupants = owner.plugin["xep_0045"].get_roster

    # ---- rename ----
    n_notice = len(app.notices)
    r = await app.call("set_group_card", group_id=gid, user_id=me, card="helper")
    check("set_group_card on the bot -> ok", r["retcode"] == 0, r)
    await until(lambda: "helper" in (occupants(room) or []) and "gwbot" not in (occupants(room) or []))
    check("room shows the new nickname, old one gone",
          "helper" in (occupants(room) or []) and "gwbot" not in (occupants(room) or []), occupants(room))
    r = await app.call("get_group_member_info", group_id=gid, user_id=me)
    check("get_group_member_info for the bot shows the new nickname",
          r["retcode"] == 0 and r["data"]["card"] == "helper", r)
    k = len(seen)
    r = await app.call("send_group_msg", group_id=gid, message="after rename")
    got = await until(lambda: next((m for m in seen[k:] if m.name == "message" and m["body"] == "after rename"), None))
    check("messages go out under the new nickname", got is not None and got["from"].resource == "helper",
          got and got["from"])
    k = len(app.events)
    owner.send_message(mto=room, mbody="helper: ping", mtype="groupchat")
    ev = await until(lambda: next((e for e in app.events[k:] if e.get("raw_message", "").endswith("ping")), None))
    check("'newnick: ...' is a mention of the bot", ev is not None and ev["message"][0]["type"] == "at", ev)
    await asyncio.sleep(1)
    check("own echo under the new nickname is not reported as a message",
          not any(e.get("raw_message") == "after rename" for e in app.events), [e.get("raw_message") for e in app.events])
    check("bot rename sends no member notices",
          not [x for x in app.notices[n_notice:] if x.get("notice_type", "").startswith("group_")],
          app.notices[n_notice:])

    # ---- someone else takes the old nickname: their messages are not mistaken for the bot's ----
    other = await peer(f"coth{t}")
    await other.join(room, "gwbot")
    k = len(app.events)
    other.send_message(mto=room, mbody="i am the new gwbot", mtype="groupchat")
    ev = await until(lambda: next((e for e in app.events[k:] if e.get("raw_message") == "i am the new gwbot"), None))
    check("a member using the bot's old nickname is reported normally", ev is not None and ev["user_id"] != me, ev)
    other.leave(room, "gwbot")
    await until(lambda: "gwbot" not in (occupants(room) or []))

    # ---- refused ----
    r = await app.call("set_group_card", group_id=gid, user_id=await gw.idmap.to_int("user", f"{room}/owner"),
                       card="x")
    check("set_group_card on another member -> failed", r["status"] == "failed", r)
    r = await app.call("set_group_card", group_id=gid, user_id=me, card="owner")
    check("nickname already taken -> failed", r["status"] == "failed", r)
    check("nickname unchanged after a refused rename", gw.xmpp.my_nick(room) == "helper"
          and "helper" in (occupants(room) or []), (gw.xmpp.my_nick(room), occupants(room)))
    r = await app.call("set_group_card", group_id=999999, user_id=me, card="x")
    check("unknown group -> failed", r["status"] == "failed", r)

    # ---- restart keeps the nickname ----
    await gw.stop()
    await until(lambda: "helper" not in (occupants(room) or []))
    gw = await start_gw(cfg, app, room)
    await until(lambda: "helper" in (occupants(room) or []))
    check("nickname kept after restart", "helper" in (occupants(room) or []) and "gwbot" not in (occupants(room) or []),
          occupants(room))

    # ---- room name ----
    r = await app.call("set_group_name", group_id=gid, group_name="Renamed room")
    check("set_group_name without owner rights -> failed", r["status"] == "failed", r)
    await owner.plugin["xep_0045"].set_affiliation(room, "owner", jid=bot[0])
    r = await app.call("set_group_name", group_id=gid, group_name="Renamed room")
    check("set_group_name as owner -> ok", r["retcode"] == 0, r)
    r = await app.call("get_group_info", group_id=gid)
    check("get_group_info shows the new name", r["retcode"] == 0 and r["data"]["group_name"] == "Renamed room", r)
    r = await app.call("set_group_name", group_id=gid, group_name="  ")
    check("empty group_name -> failed", r["status"] == "failed", r)

    # ---- empty card restores the configured nick ----
    r = await app.call("set_group_card", group_id=gid, user_id=me, card="")
    await until(lambda: "gwbot" in (occupants(room) or []))
    check("empty card restores the configured nickname", r["retcode"] == 0 and "gwbot" in (occupants(room) or []),
          (r, occupants(room)))
    await gw.stop()

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
