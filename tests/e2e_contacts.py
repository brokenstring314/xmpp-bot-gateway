"""Contact (subscription) requests in the three `xmpp.contact_requests` modes, end to end:
report (request.friend + set_friend_add_request), accept (auto + friend_add notice), reject,
plus delete_friend and requests that arrive while the gateway is offline.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_contacts.py`.
"""
import asyncio, logging, os, secrets, subprocess, sys

import slixmpp
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from harness import MockApp, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6301
RUN = "tests/prosody/run"
WORK = "data/e2e_contacts"


def register(name):
    pw = secrets.token_urlsafe(12)
    cfg = f"{RUN}/prosody.cfg.lua"
    subprocess.run(["prosodyctl", "--config", cfg, "deluser", f"{name}@test.local"], capture_output=True)
    subprocess.run(["prosodyctl", "--config", cfg, "register", name, "test.local", pw], check=True,
                   capture_output=True)
    return f"{name}@test.local", pw


class Peer(slixmpp.ClientXMPP):
    """A contact that records the subscription presences it gets and accepts the bot's request back."""
    def __init__(self, jid, pw):
        super().__init__(jid, pw)
        self.subs: list[tuple[str, str]] = []
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)
        for t in ("subscribe", "subscribed", "unsubscribe", "unsubscribed"):
            self.add_event_handler(f"presence_{t}", lambda p, t=t: self.subs.append((t, p["from"].bare)))
        self.enable_starttls = self.enable_direct_tls = False
        self.enable_plaintext = True
        self.plugin["feature_mechanisms"].unencrypted_plain = True

    async def _start(self, _):
        self.send_presence()
        await self.get_roster()
        self.ready.set()

    def ask(self, to, text=""):
        self.send_presence(pto=to, ptype="subscribe", pstatus=text or None)

    def has(self, kind, frm):
        return (kind, frm) in self.subs


async def peer(name):
    jid, pw = register(name)
    p = Peer(jid, pw)
    p.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(p.ready.wait(), 20)
    return p


def write_cfg(bot, mode, db):
    cfg = {"xmpp": {"jid": bot[0], "password": bot[1], "server": "127.0.0.1", "port": 15222, "tls": "none",
                    "rate_limit": 0, "contact_requests": mode, "mam_catchup": False},
           "storage": {"db": db},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/{mode}.toml"
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

    # ---- report ----
    gw = await start_gw(write_cfg(bot, "report", db), app)
    alice = await peer(f"calice{t}")
    alice.ask(bot[0], "hi bot, it's alice")
    rq = await until(lambda: next((r for r in app.requests if r.get("flag") == alice.boundjid.bare), None))
    check("report: subscription request -> request.friend",
          rq and rq["request_type"] == "friend" and rq["comment"] == "hi bot, it's alice"
          and rq["user_id"] == await gw.idmap.to_int("user", alice.boundjid.bare), rq)
    await asyncio.sleep(1)
    check("report: nothing is answered before the framework decides",
          not alice.has("subscribed", bot[0]) and not alice.has("unsubscribed", bot[0]), alice.subs)
    n = len(app.notices)
    r = await app.call("set_friend_add_request", flag=rq["flag"], approve=True, remark="Alice")
    check("report: set_friend_add_request approve -> ok", r["status"] == "ok", r)
    check("report: approved contact gets subscribed",
          await until(lambda: alice.has("subscribed", bot[0])), alice.subs)
    check("report: bot subscribes back", await until(lambda: alice.has("subscribe", bot[0])), alice.subs)
    nt = await until(lambda: next((x for x in app.notices[n:] if x["notice_type"] == "friend_add"), None))
    check("report: friend_add notice after approval", nt and nt["user_id"] == rq["user_id"], app.notices[n:])
    await until(lambda: gw.xmpp.client_roster[alice.boundjid.bare]["subscription"] == "both", 10)
    fl = (await app.call("get_friend_list"))["data"]
    me = next((f for f in fl if f["user_id"] == rq["user_id"]), None)
    check("report: approved contact in friend list with remark", me and me["remark"] == "Alice", fl)
    r = await app.call("set_friend_add_request", flag=rq["flag"], approve=True)
    check("report: answering the same request twice fails", r["status"] == "failed", r)
    r = await app.call("set_friend_add_request", flag="nobody@test.local", approve=True)
    check("report: unknown flag fails", r["status"] == "failed", r)

    bob = await peer(f"cbob{t}")
    bob.ask(bot[0])
    rq2 = await until(lambda: next((r for r in app.requests if r.get("flag") == bob.boundjid.bare), None))
    n = len(app.notices)
    r = await app.call("set_friend_add_request", flag=bob.boundjid.bare, approve=False)
    check("report: reject -> ok", rq2 and r["status"] == "ok", r)
    check("report: rejected contact gets unsubscribed",
          await until(lambda: bob.has("unsubscribed", bot[0])), bob.subs)
    await asyncio.sleep(1)
    fl = (await app.call("get_friend_list"))["data"]
    check("report: rejected contact not a friend, no friend_add",
          rq2 and all(f["user_id"] != rq2["user_id"] for f in fl)
          and not any(x["notice_type"] == "friend_add" for x in app.notices[n:]), (fl, app.notices[n:]))
    r = await app.call("delete_friend", user_id=rq2["user_id"])
    check("delete_friend on a non-contact fails", r["status"] == "failed", r)

    r = await app.call("delete_friend", user_id=rq["user_id"])
    check("delete_friend -> ok", r["status"] == "ok", r)
    check("delete_friend: contact loses the subscription",
          await until(lambda: alice.has("unsubscribe", bot[0]) or alice.has("unsubscribed", bot[0])), alice.subs)
    await until(lambda: alice.boundjid.bare not in gw.xmpp.client_roster
                or gw.xmpp.client_roster[alice.boundjid.bare]["subscription"] in ("none", "remove"), 10)
    fl = (await app.call("get_friend_list"))["data"]
    check("delete_friend: gone from friend list", all(f["user_id"] != rq["user_id"] for f in fl), fl)

    # request sent while the gateway is offline: the server redelivers it at the next login
    await gw.stop()
    carol = await peer(f"ccarol{t}")
    carol.ask(bot[0], "offline hello")
    await asyncio.sleep(1)
    gw = await start_gw(write_cfg(bot, "report", db), app)
    rq3 = await until(lambda: next((r for r in app.requests if r.get("flag") == carol.boundjid.bare), None))
    check("report: request made while offline is reported after login",
          rq3 and rq3["comment"] == "offline hello", app.requests)
    r = await app.call("set_friend_add_request", flag=carol.boundjid.bare, approve=True)
    check("report: offline request can be approved",
          r["status"] == "ok" and await until(lambda: carol.has("subscribed", bot[0])), (r, carol.subs))
    await gw.stop()

    # ---- accept ----
    gw = await start_gw(write_cfg(bot, "accept", db), app)
    dave = await peer(f"cdave{t}")
    n, m = len(app.notices), len(app.requests)
    dave.ask(bot[0])
    check("accept: request approved automatically", await until(lambda: dave.has("subscribed", bot[0])), dave.subs)
    check("accept: bot subscribes back", await until(lambda: dave.has("subscribe", bot[0])), dave.subs)
    nt = await until(lambda: next((x for x in app.notices[n:] if x["notice_type"] == "friend_add"), None))
    check("accept: friend_add notice, no request event",
          nt and nt["user_id"] == await gw.idmap.to_int("user", dave.boundjid.bare) and len(app.requests) == m,
          (app.notices[n:], app.requests[m:]))
    await gw.stop()

    # ---- reject ----
    gw = await start_gw(write_cfg(bot, "reject", db), app)
    eve = await peer(f"ceve{t}")
    n, m = len(app.notices), len(app.requests)
    eve.ask(bot[0])
    check("reject: request refused automatically", await until(lambda: eve.has("unsubscribed", bot[0])), eve.subs)
    await asyncio.sleep(1)
    check("reject: no events, no subscription back",
          len(app.notices) == n and len(app.requests) == m and not eve.has("subscribe", bot[0]),
          (app.notices[n:], app.requests[m:], eve.subs))
    await gw.stop()

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
