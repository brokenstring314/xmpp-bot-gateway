"""Group member join/leave notices, end to end: notice.group_increase / group_decrease (leave, kick, kick_me),
with no events for the member list received on joining, status updates or nickname changes.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_members.py`.
"""
import asyncio, logging, os, secrets, subprocess, sys

import slixmpp
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from harness import MockApp, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6303
RUN = "tests/prosody/run"
WORK = "data/e2e_members"


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


def member_notices(app, n):
    return [x for x in app.notices[n:] if x.get("notice_type") in ("group_increase", "group_decrease")]


async def main():
    logging.basicConfig(level=logging.WARNING)
    os.makedirs(WORK, exist_ok=True)
    t = secrets.token_hex(3)
    room = f"members{t}@conference.test.local"
    bot = register(f"mbot{t}")
    db = f"{WORK}/gateway.db"
    if os.path.exists(db):
        os.unlink(db)
    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wa)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    owner = await peer(f"mown{t}")
    await owner.join(room, "owner")
    alice = await peer(f"mali{t}")
    await alice.join(room, "alice")

    n = len(app.notices)
    gw = await start_gw(write_cfg(bot, "on", db, room), app, room)
    gid = await gw.idmap.to_int("group", room)
    await asyncio.sleep(1.5)
    check("no events for members already in the room when the bot joins", member_notices(app, n) == [],
          member_notices(app, n))

    # ---- join ----
    bob = await peer(f"mbob{t}")
    n = len(app.notices)
    await bob.join(room, "bob")
    ev = await until(lambda: member_notices(app, n) and member_notices(app, n)[0])
    bob_id = await gw.idmap.to_int("user", gw.xmpp.occupant_ident(room, "bob"))
    check("member joins -> group_increase approve",
          ev and ev["notice_type"] == "group_increase" and ev["sub_type"] == "approve"
          and ev["group_id"] == gid and ev["user_id"] == bob_id and ev["operator_id"] == 0, ev)
    k = len(app.events)
    bob.send_message(mto=room, mbody="hi from bob", mtype="groupchat")
    msg = await until(lambda: len(app.events) > k and app.events[-1])
    check("join notice user_id matches the member's message user_id", msg and msg["user_id"] == bob_id, msg)

    # ---- status update and nickname change: no events ----
    n = len(app.notices)
    bob.send_presence(pto=f"{room}/bob", pshow="away")
    bob.send_presence(pto=f"{room}/bobby")
    await until(lambda: "bobby" in gw.xmpp._present.get(room, set()))
    await asyncio.sleep(1.5)
    check("status update and nickname change send no events", member_notices(app, n) == [], member_notices(app, n))

    # ---- leave ----
    n = len(app.notices)
    alice.leave(room, "alice")
    ev = await until(lambda: member_notices(app, n) and member_notices(app, n)[0])
    check("member leaves -> group_decrease leave, operator = member",
          ev and ev["notice_type"] == "group_decrease" and ev["sub_type"] == "leave"
          and ev["operator_id"] == ev["user_id"] != 0, ev)

    # ---- kick ----
    n = len(app.notices)
    bobby_id = await gw.idmap.to_int("user", gw.xmpp.occupant_ident(room, "bobby"))
    owner_id = await gw.idmap.to_int("user", gw.xmpp.occupant_ident(room, "owner"))
    await owner.plugin["xep_0045"].set_role(room, "bobby", "none", reason="bye")
    ev = await until(lambda: member_notices(app, n) and member_notices(app, n)[0])
    check("member kicked -> group_decrease kick",
          ev and ev["sub_type"] == "kick" and ev["user_id"] == bobby_id, (ev, bobby_id))
    check("kick operator_id is the moderator", ev and ev["operator_id"] == owner_id, (ev, owner_id))

    # ---- restart: rejoin does not replay the member list ----
    await gw.stop()
    carol = await peer(f"mcar{t}")
    await carol.join(room, "carol")      # joins while the bot is away: not reported
    n = len(app.notices)
    gw = await start_gw(write_cfg(bot, "on", db, room), app, room)
    await asyncio.sleep(1.5)
    check("rejoin after restart sends no events", member_notices(app, n) == [], member_notices(app, n))

    # ---- bot kicked ----
    n = len(app.notices)
    await owner.plugin["xep_0045"].set_role(room, "gwbot", "none")
    ev = await until(lambda: member_notices(app, n) and member_notices(app, n)[0])
    check("bot kicked -> group_decrease kick_me",
          ev and ev["sub_type"] == "kick_me" and ev["user_id"] == ev["self_id"] and ev["operator_id"] == owner_id,
          ev)
    r = await app.call("send_group_msg", group_id=gid, message="still here?")
    check("after being kicked, sending to the room fails", r["status"] == "failed", r)
    await gw.stop()

    # ---- member_notices = false ----
    gw = await start_gw(write_cfg(bot, "off", db, room, member_notices=False), app, room)
    n = len(app.notices)
    dave = await peer(f"mdav{t}")
    await dave.join(room, "dave")
    dave.leave(room, "dave")
    await asyncio.sleep(2)
    check("member_notices = false: no events", member_notices(app, n) == [], member_notices(app, n))
    await gw.stop()

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
