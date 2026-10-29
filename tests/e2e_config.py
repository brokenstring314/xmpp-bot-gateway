"""Every configuration branch, end to end: each case starts a gateway with one setting changed and checks
the behaviour it promises (and, where it matters, the opposite value too).

Covered here: [onebot_v11] enabled, access_token (global / per endpoint / empty), message_format,
heartbeat_interval, event_max_age, reconnect_max; [xmpp] tls (starttls / direct / untrusted cert), rooms +
nick, join_timeout, status, accept_invites (roster / all / none) + admins, rate_limit, max_body_chars,
mam_catchup, mam_max_messages, mam_max_age, reconnect_max; [media] local_file_dirs, max_size_mb (outgoing
and incoming), http_timeout, max_dir_mb; [storage] keep_days; [satori] enabled.
OMEMO options are in e2e_config_omemo.py, [log] and parsing in test_config.py.

Usage: start tests/prosody/run.sh with setsid, then run `python3 -u tests/e2e_config.py`.
The XMPP reconnect case restarts Prosody, so it runs last.
"""
import asyncio, json, logging, os, secrets, shutil, sqlite3, subprocess, sys, time

import slixmpp
import tomli_w
from aiohttp import web

sys.path[:0] = ["src", "tests"]
from harness import User, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402
from xmpp_gateway.xmpp import media  # noqa: E402

RUN = "tests/prosody/run"
WORK = os.path.abspath("data/e2e_config")
CA = os.path.abspath(f"{RUN}/certs/ca.crt")
P1, P2, P3, BAIT = 6411, 6412, 6413, 6414
ACC = json.load(open(f"{RUN}/accounts.json"))
BOT, USER = ACC["bot"]["jid"], ACC["user"]["jid"]
log = logging.getLogger("e2e_config")


class Lines(logging.Handler):
    """Collects the gateway's log lines (some branches are only observable there)."""
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def has(self, s, start=0):
        return any(s in x for x in self.lines[start:])


LOG = Lines()


class App:
    """Mock OneBot v11 framework that also records the handshake headers and meta events."""
    def __init__(self, port):
        self.port, self.runner = port, None
        self.ws, self.events, self.meta, self.headers, self.pending, self.n = None, [], [], [], {}, 0
        self.conn_times: list[float] = []

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self.headers.append(dict(request.headers))
        self.conn_times.append(time.time())
        async for m in ws:
            d = json.loads(m.data)
            if "echo" in d and "post_type" not in d:
                fut = self.pending.pop(d["echo"], None)
                if fut and not fut.done():
                    fut.set_result(d)
            elif d.get("post_type") == "message":
                self.events.append(d)
            elif d.get("post_type") == "meta_event":
                self.meta.append(d)
        return ws

    async def start(self):
        a = web.Application()
        a.router.add_get("/ws", self.handler)
        self.runner = web.AppRunner(a, shutdown_timeout=0.5)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner, self.ws = None, None

    async def call(self, action, **params):
        self.n += 1
        echo = f"e{self.n}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}))
        return await asyncio.wait_for(fut, 20)

    def texts(self, k=0):
        return [e["raw_message"] for e in self.events[k:]]

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/ws"


def merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Run:
    """One gateway with a config built from the test defaults plus `over`."""
    def __init__(self, name, over, apps=()):
        self.name = name
        self.lock_free(name)
        base = {"xmpp": {**ACC["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot",
                         "rooms": [], "rate_limit": 0, "mam_catchup": False, "reconnect_max": 2},
                "storage": {"db": f"{WORK}/{name}.db"},
                "media": {"dir": f"{WORK}/{name}-media"},
                "onebot_v11": {"enabled": True, "reverse_ws": [a.url for a in apps], "reconnect_max": 1}}
        self.cfg = merge(base, over)
        self.path = f"{WORK}/{name}.toml"
        open(self.path, "wb").write(tomli_w.dumps(self.cfg).encode())
        self.gw: Gateway | None = None

    @staticmethod
    def lock_free(name):
        try:
            os.unlink(f"{WORK}/{name}.db.lock")
        except FileNotFoundError:
            pass

    async def __aenter__(self):
        self.gw = Gateway(self.path)
        await self.gw.start()
        return self.gw

    async def __aexit__(self, *exc):
        await self.gw.stop()
        await asyncio.sleep(0.5)


def plain_client(jid, pw, cls=User):
    c = cls(jid, pw)
    c.enable_starttls = c.enable_direct_tls = False
    c.enable_plaintext = True
    c["feature_mechanisms"].unencrypted_plain = True
    c.connect(host="127.0.0.1", port=15222)
    return c


def register(name):
    pw = secrets.token_urlsafe(12)
    subprocess.run(["prosodyctl", "--config", f"{RUN}/prosody.cfg.lua", "deluser", f"{name}@test.local"],
                   capture_output=True)
    subprocess.run(["prosodyctl", "--config", f"{RUN}/prosody.cfg.lua", "register", name, "test.local", pw],
                   check=True, capture_output=True)
    return f"{name}@test.local", pw


async def online(gw, app=None, rooms=(), timeout=20):
    return await until(lambda: gw.xmpp.online and (app is None or app.ws is not None) and
                       all(r in gw.xmpp.rooms() for r in rooms), timeout)


async def quiet(app, secs=2.0):
    """Wait until no new events arrived for `secs` (drains offline backlog from earlier cases)."""
    n, last = -1, time.time()
    while time.time() - last < secs:
        if len(app.events) != n:
            n, last = len(app.events), time.time()
        await asyncio.sleep(0.2)


def tag():
    return secrets.token_hex(3)


async def make_room(u, room, nick="user"):
    """The user creates the room and stays in it, so it lives on while the gateway is offline."""
    muc = u.plugin["xep_0045"]
    res = await muc.join_muc_wait(slixmpp.JID(room), nick, maxstanzas=0, timeout=10)
    pres = res[0] if isinstance(res, tuple) else res
    if 201 in pres["muc"]["status_codes"]:
        await muc.set_room_config(room, u.plugin["xep_0004"].make_form(ftype="submit"))


def invite(c, room):
    msg = c.make_message(mto=BOT, mtype="normal")
    x = slixmpp.xmlstream.ET.SubElement(msg.xml, "{jabber:x:conference}x")
    x.set("jid", room)
    msg.send()


# ------------------------------------------------------------------ OneBot v11
async def case_onebot(u):
    a, b, c = App(P1), App(P2), App(P3)
    for x in (a, b, c):
        await x.start()
    over = {"onebot_v11": {"access_token": "global-tok", "heartbeat_interval": 0,
                           "reverse_ws": [a.url, {"url": b.url, "access_token": "b-tok"},
                                          {"url": c.url, "access_token": ""}]}}
    async with Run("onebot-tokens", over) as gw:
        await online(gw)
        await until(lambda: None not in (a.ws, b.ws, c.ws), 15)
        await quiet(a)
        auth = [x.headers[-1].get("Authorization") if x.headers else "no connection" for x in (a, b, c)]
        check("access_token: endpoint without its own token uses the global one", auth[0] == "Bearer global-tok", auth)
        check("access_token: per-endpoint token overrides the global one", auth[1] == "Bearer b-tok", auth)
        check("access_token: per-endpoint empty token sends no Authorization", auth[2] is None, auth)
        check("reverse_ws: every endpoint gets X-Self-ID",
              all(x.headers and x.headers[-1].get("X-Self-ID") for x in (a, b, c)))
        t = tag()
        ks = [len(x.events) for x in (a, b, c)]
        u.send_message(mto=BOT, mbody=f"fanout {t} &[x]", mtype="chat")
        got = await until(lambda: all(f"fanout {t}" in " ".join(x.texts(k)) for x, k in zip((a, b, c), ks)), 10)
        check("reverse_ws: each event reaches every endpoint", got, [x.texts(k) for x, k in zip((a, b, c), ks)])
        ev = next((e for e in a.events[ks[0]:] if f"fanout {t}" in e["raw_message"]), None)
        check("message_format=array (default): message is a segment list",
              ev and isinstance(ev["message"], list) and ev["message"][0]["data"]["text"] == f"fanout {t} &[x]",
              ev and ev["message"])
        hb_before = sum(m.get("meta_event_type") == "heartbeat" for m in a.meta)
        await asyncio.sleep(3)
        check("heartbeat_interval=0: no heartbeat events",
              sum(m.get("meta_event_type") == "heartbeat" for m in a.meta) == hb_before == 0)
    for x in (a, b, c):
        await x.stop()

    a = App(P1)
    await a.start()
    async with Run("onebot-string", {"onebot_v11": {"message_format": "string", "heartbeat_interval": 1000}}, [a]) as gw:
        await online(gw, a)
        await quiet(a)
        t, k = tag(), len(a.events)
        u.send_message(mto=BOT, mbody=f"fmt {t} &[x]", mtype="chat")
        ev = await until(lambda: next((e for e in a.events[k:] if t in e["raw_message"]), None), 10)
        check("message_format=string: message is an escaped CQ string",
              ev and ev["message"] == f"fmt {t} &amp;&#91;x&#93;", ev and ev["message"])
        if ev:
            r = await a.call("get_msg", message_id=ev["message_id"])
            check("message_format=string: get_msg also returns a CQ string",
                  isinstance((r.get("data") or {}).get("message"), str), r)
        await asyncio.sleep(2.5)
        hbs = [m for m in a.meta if m.get("meta_event_type") == "heartbeat"]
        check("heartbeat_interval=1000: heartbeats arrive with interval 1000",
              len(hbs) >= 2 and hbs[-1]["interval"] == 1000, len(hbs))
    await a.stop()

    d = App(P2)
    await d.start()
    async with Run("onebot-disabled", {"onebot_v11": {"enabled": False}}, [d]) as gw:
        ok = await online(gw)
        await asyncio.sleep(3)
        check("onebot_v11.enabled=false: XMPP still logs in, no WS connection", ok and not d.headers, d.headers)
    await d.stop()


async def case_event_age(u):
    for age, name in ((2, "event-age-2"), (0, "event-age-0")):
        a = App(P1)
        await a.start()
        async with Run(name, {"onebot_v11": {"event_max_age": age, "reconnect_max": 1}}, [a]) as gw:
            await online(gw, a)
            await quiet(a)
            await a.stop()
            t = tag()
            u.send_message(mto=BOT, mbody=f"old {t}", mtype="chat")
            await asyncio.sleep(4)
            u.send_message(mto=BOT, mbody=f"new {t}", mtype="chat")
            await asyncio.sleep(0.5)
            a.ws = None
            t0 = time.time()
            await a.start()
            back = await until(lambda: a.ws is not None, 10)
            if age:
                check("onebot reconnect_max=1: reconnects within ~1 s of the framework coming back",
                      back and a.conn_times[-1] - t0 < 2.5, back and round(a.conn_times[-1] - t0, 1))
            await until(lambda: f"new {t}" in a.texts(), 10)
            await asyncio.sleep(1)
            if age:
                check("event_max_age=2: events queued longer than 2 s are dropped",
                      f"new {t}" in a.texts() and f"old {t}" not in a.texts(), a.texts())
            else:
                check("event_max_age=0: every queued event is delivered",
                      f"new {t}" in a.texts() and f"old {t}" in a.texts(), a.texts())
        await a.stop()


# ------------------------------------------------------------------ XMPP
async def case_tls():
    old = os.environ.get("SSL_CERT_FILE")
    try:
        os.environ["SSL_CERT_FILE"] = CA     # read when the SSL context is created (in Gateway())
        for mode, port in (("starttls", 15222), ("direct", 15223)):
            async with Run(f"tls-{mode}", {"xmpp": {"tls": mode, "port": port}, "onebot_v11": {"enabled": False}}) as gw:
                ok = await online(gw, timeout=15)
                ssl_obj = gw.xmpp.transport.get_extra_info("ssl_object") if gw.xmpp.transport else None
                check(f"tls={mode}: logs in over an encrypted, verified connection",
                      ok and ssl_obj is not None, (ok, ssl_obj))
        os.environ.pop("SSL_CERT_FILE")
        async with Run("tls-untrusted", {"xmpp": {"tls": "starttls"}, "onebot_v11": {"enabled": False}}) as gw:
            ok = await online(gw, timeout=6)
            check("tls=starttls: a certificate from an untrusted CA is refused", not ok)
    finally:
        if old is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = old


async def case_rooms_status(u):
    room = f"cfg-{tag()}@conference.test.local"
    await make_room(u, room)
    seen: list = []
    u.add_event_handler("presence_available", lambda p: seen.append(p))
    over = {"xmpp": {"rooms": ["ghost@test.local", room], "nick": "cfgbot", "join_timeout": 3,
                     "status": "cfg-status-ok"}, "onebot_v11": {"enabled": False}}
    t0 = time.time()
    async with Run("rooms", over) as gw:
        ok = await online(gw, rooms=[room], timeout=15)
        took = time.time() - t0
        check("join_timeout=3: a room that never answers is given up after ~3 s, the next one is joined",
              ok and 2.5 < took < 10, round(took, 1))
        check("rooms: the unanswered room is not listed as joined", "ghost@test.local" not in gw.xmpp.rooms())
        nicks = u.plugin["xep_0045"].get_roster(room) or []
        check("nick: the bot joins under the configured nick", "cfgbot" in nicks, nicks)
        u.send_presence_subscription(pto=BOT)       # the bot authorizes; its presence then reaches the user
        st = await until(lambda: next((p["status"] for p in seen if p["from"].bare == BOT and p["status"]), None), 10)
        check("status: the bot's presence carries the configured status text", st == "cfg-status-ok", st)
    u.plugin["xep_0045"].leave_muc(slixmpp.JID(room), "user")


async def case_invites(u):
    mal_jid, mal_pw = register(f"mal{tag()}")
    mal = plain_client(mal_jid, mal_pw)
    await asyncio.wait_for(mal.ready.wait(), 15)
    cases = [("roster", [], mal, False, "accept_invites=roster: a stranger's invite is ignored"),
             ("roster", [], u, True, "accept_invites=roster: a roster contact's invite is accepted"),
             ("roster", [mal_jid], mal, True, "admins: an admin's invite is accepted under roster"),
             ("all", [], mal, True, "accept_invites=all: a stranger's invite is accepted"),
             ("none", [mal_jid], mal, False, "accept_invites=none: even an admin's invite is ignored")]
    for policy, admins, who, want, name in cases:
        async with Run(f"invite-{policy}", {"xmpp": {"accept_invites": policy, "admins": admins},
                                            "onebot_v11": {"enabled": False}}) as gw:
            await online(gw)
            await asyncio.sleep(1)
            room = f"inv-{tag()}@conference.test.local"
            invite(who, room)
            joined = await until(lambda: room in gw.xmpp.rooms(), 6 if want else 4)
            check(name, bool(joined) == want, gw.xmpp.rooms())
    mal.disconnect()


async def case_limits(u):
    a = App(P1)
    await a.start()
    for limit in (3, 0):
        async with Run(f"rate-{limit}", {"xmpp": {"rate_limit": limit}}, [a]) as gw:
            await online(gw, a)
            await quiet(a)
            t, k = tag(), len(a.events)
            for i in range(6):
                u.send_message(mto=BOT, mbody=f"burst {t} {i}", mtype="chat")
            await asyncio.sleep(3)
            n = sum(f"burst {t}" in x for x in a.texts(k))
            check(f"rate_limit={limit}: {'3 of 6 messages forwarded' if limit else 'all 6 messages forwarded'}",
                  n == (3 if limit else 6), n)
    long_body = "0123456789" + "x" * 4990
    for n_chars in (10, 0):
        async with Run(f"body-{n_chars}", {"xmpp": {"max_body_chars": n_chars}}, [a]) as gw:
            await online(gw, a)
            await quiet(a)
            k = len(a.events)
            u.send_message(mto=BOT, mbody=long_body, mtype="chat")
            ev = await until(lambda: next((e for e in a.events[k:] if e["raw_message"].startswith("0123")), None), 10)
            raw = ev["raw_message"] if ev else ""
            want = "0123456789" if n_chars else long_body
            check(f"max_body_chars={n_chars}: {'truncated to 10 chars' if n_chars else 'not truncated'}",
                  raw == want, len(raw))
    await a.stop()


async def case_mam(u):
    room = f"mam-{tag()}@conference.test.local"
    await make_room(u, room)
    a = App(P1)
    await a.start()

    def say(prefix, n):
        for i in range(n):
            u.send_message(mto=room, mbody=f"{prefix} {i}", mtype="groupchat")

    def count(prefix, k):
        return sum(x.startswith(prefix) for x in a.texts(k))

    # every run shares one database: the archive checkpoint must survive between runs
    first = Run("mam-first", {"xmpp": {"rooms": [room], "mam_catchup": True}, "storage": {"db": f"{WORK}/mam.db"}}, [a])
    async with first as gw:
        await online(gw, a, [room])           # the first run only records "now" as the starting point
        await asyncio.sleep(2)
    t = tag()
    say(f"off1-{t}", 5)
    await asyncio.sleep(1)

    async def run_db(name, xmpp, prefix):
        r = Run(name, {"xmpp": {"rooms": [room], **xmpp}}, [a])
        r.cfg["storage"]["db"] = f"{WORK}/mam.db"
        open(r.path, "wb").write(tomli_w.dumps(r.cfg).encode())
        async with r as gw:
            k = len(a.events)
            await online(gw, a, [room])
            await asyncio.sleep(4)
            return count(prefix, k)

    n = await run_db("mam-off", {"mam_catchup": False}, f"off1-{t}")
    check("mam_catchup=false: messages sent while offline are not fetched", n == 0, n)
    n = await run_db("mam-max2", {"mam_catchup": True, "mam_max_messages": 2}, f"off1-{t}")
    check("mam_max_messages=2: at most 2 archived messages are fetched", n == 2, n)
    say(f"off2-{t}", 3)
    await asyncio.sleep(6)   # well past mam_max_age, also covering the server's whole-second timestamps
    n = await run_db("mam-age1", {"mam_catchup": True, "mam_max_age": 1}, f"off2-{t}")
    check("mam_max_age=1: archived messages older than 1 s are skipped", n == 0, n)
    say(f"off3-{t}", 3)
    await asyncio.sleep(1)
    n = await run_db("mam-on", {"mam_catchup": True}, f"off3-{t}")
    check("mam_catchup=true (defaults): offline messages are fetched", n == 3, n)
    await a.stop()
    u.plugin["xep_0045"].leave_muc(slixmpp.JID(room), "user")


# ------------------------------------------------------------------ media / storage / satori
async def case_media(u):
    a = App(P1)
    await a.start()
    allowed = f"{WORK}/allowed"
    os.makedirs(allowed, exist_ok=True)
    small, big = f"{allowed}/small.png", f"{allowed}/big.png"
    open(small, "wb").write(b"\x89PNG\r\n\x1a\n" + os.urandom(2000))
    open(big, "wb").write(b"\x89PNG\r\n\x1a\n" + os.urandom(1_200_000))
    outside = f"{WORK}/outside.png"
    shutil.copy(small, outside)

    blobs: dict[str, bytes] = {}
    slow = {"delay": 0.0}

    async def serve(request):
        await asyncio.sleep(slow["delay"])
        return web.Response(body=blobs.get(request.match_info["name"], b""))
    srv = web.Application()
    srv.router.add_get("/{name}", serve)
    sr = web.AppRunner(srv, shutdown_timeout=0.5)
    await sr.setup()
    await web.TCPSite(sr, "127.0.0.1", BAIT).start()

    def aesgcm_link(name, size):
        payload, frag = media.aesgcm_encrypt(b"\x89PNG\r\n\x1a\n" + os.urandom(size))
        blobs[name] = payload
        return media.aesgcm_url(f"http://127.0.0.1:{BAIT}/{name}", frag)

    over = {"media": {"local_file_dirs": [allowed], "max_size_mb": 1, "http_timeout": 2, "max_dir_mb": 1,
                      "aesgcm_insecure_http": True, "allow_private_hosts": True}}
    async with Run("media", over, [a]) as gw:
        await online(gw, a)
        await quiet(a)
        k = len(u.inbox)
        r1 = await a.call("send_private_msg", user_id=await gw.idmap.to_int("user", USER),
                          message=[{"type": "image", "data": {"file": f"file://{small}"}}])
        got = await until(lambda: u.got(lambda m: "http://127.0.0.1:15280/" in m["body"], k), 10)
        check("local_file_dirs: a file inside an allowed directory is uploaded", r1["status"] == "ok" and got, r1)
        r2 = await a.call("send_private_msg", user_id=await gw.idmap.to_int("user", USER),
                          message=[{"type": "image", "data": {"file": f"file://{outside}"}}])
        check("local_file_dirs: a file outside the allowed directories is refused", r2["status"] == "failed", r2)
        r3 = await a.call("send_private_msg", user_id=await gw.idmap.to_int("user", USER),
                          message=[{"type": "image", "data": {"file": f"file://{big}"}}])
        check("max_size_mb=1: an outgoing 1.2 MB file is refused", r3["status"] == "failed", r3)

        def img_events(k):
            return [e for e in a.events[k:] if isinstance(e["message"], list)
                    and any(s["type"] == "image" for s in e["message"])]
        k = len(a.events)
        u.send_message(mto=BOT, mbody=aesgcm_link("big.png", 1_200_000), mtype="chat")
        await asyncio.sleep(3)
        check("max_size_mb=1: an incoming 1.2 MB encrypted file is not downloaded", not img_events(k),
              a.texts(k))
        slow["delay"] = 5
        k, t0 = len(a.events), time.time()
        u.send_message(mto=BOT, mbody=aesgcm_link("slow.png", 1000), mtype="chat")
        ev = await until(lambda: a.events[k:], 8)
        check("http_timeout=2: a stalled download is abandoned after ~2 s",
              ev and not img_events(k) and time.time() - t0 < 5, (round(time.time() - t0, 1), a.texts(k)))
        slow["delay"] = 0
        for i in range(4):
            k = len(a.events)
            u.send_message(mto=BOT, mbody=aesgcm_link(f"f{i}.png", 400_000), mtype="chat")
            await until(lambda: img_events(k), 10)
        files = os.listdir(f"{WORK}/media-media")
        total = sum(os.path.getsize(f"{WORK}/media-media/{f}") for f in files)
        check("max_dir_mb=1: oldest incoming files are deleted to stay under 1 MB",
              total <= 1024 * 1024 and len(files) >= 1, (len(files), total))
    await sr.cleanup()
    await a.stop()


async def case_storage(u):
    a = App(P1)
    await a.start()
    for days in (1, 0):
        name = f"keep-{days}"
        async with Run(name, {"storage": {"keep_days": days}}, [a]) as gw:
            await online(gw, a)
            await quiet(a)
            t, k = tag(), len(a.events)
            u.send_message(mto=BOT, mbody=f"keep {t}", mtype="chat")
            ev = await until(lambda: next((e for e in a.events[k:] if t in e["raw_message"]), None), 10)
        db = sqlite3.connect(f"{WORK}/{name}.db")
        db.execute("UPDATE messages SET time = time - 2 * 86400")     # pretend everything is two days old
        db.commit()
        db.close()
        async with Run(name, {"storage": {"keep_days": days}}, [a]) as gw:
            await online(gw, a)
            r = await a.call("get_msg", message_id=ev["message_id"]) if ev else {"status": "?"}
        if days:
            check("keep_days=1: a two-day-old message record is pruned on start", r["status"] == "failed", r)
        else:
            check("keep_days=0: message records are kept forever", r["status"] == "ok", r)
    await a.stop()


async def case_satori():
    n = len(LOG.lines)
    async with Run("satori", {"satori": {"enabled": True}, "onebot_v11": {"enabled": False}}) as gw:
        ok = await online(gw)
        check("satori.enabled=true: gateway still runs and warns that Satori is not implemented",
              ok and LOG.has("Satori is not implemented yet", n), LOG.lines[n:][-5:])


# ------------------------------------------------------------------ XMPP reconnect (restarts Prosody)
async def case_reconnect():
    n = len(LOG.lines)
    async with Run("reconnect", {"xmpp": {"reconnect_max": 3}, "onebot_v11": {"enabled": False}}) as gw:
        await online(gw)
        pid = int(open(f"{RUN}/prosody.pid").read().strip())
        os.kill(pid, 15)
        await asyncio.sleep(14)
        subprocess.Popen(["setsid", "bash", "tests/prosody/run.sh"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        t0 = time.time()
        back = await online(gw, timeout=20)
        took = time.time() - t0
        delays = [int(x.rsplit(" in ", 1)[1].rstrip("s")) for x in LOG.lines[n:] if "reconnecting in" in x]
        check("xmpp reconnect_max=3: backoff grows 2 s -> 3 s and stays capped at 3 s",
              delays[:1] == [2] and max(delays) == 3 and delays.count(3) >= 2, delays)
        check("xmpp reconnect_max=3: back online within ~3 s of the server returning", back and took < 7,
              round(took, 1))


async def main():
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("xmpp_gateway").addHandler(LOG)
    logging.getLogger("xmpp_gateway").setLevel(logging.DEBUG)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    u = plain_client(USER, ACC["user"]["password"])
    await asyncio.wait_for(u.ready.wait(), 15)
    only = set(filter(None, os.environ.get("CASES", "").split(",")))   # e.g. CASES=onebot,tls
    cases = {"onebot": lambda: case_onebot(u), "event_age": lambda: case_event_age(u), "tls": case_tls,
             "rooms": lambda: case_rooms_status(u), "invites": lambda: case_invites(u),
             "limits": lambda: case_limits(u), "mam": lambda: case_mam(u), "media": lambda: case_media(u),
             "storage": lambda: case_storage(u), "satori": case_satori}
    for name, make in cases.items():
        if only and name not in only:
            continue
        try:
            await make()
        except BaseException as e:  # noqa: BLE001  (SystemExit from a gateway must not end the run)
            check(f"{name} ran without errors", False, repr(e))
            log.exception(name)
    u.disconnect()
    if not only or "reconnect" in only:
        await case_reconnect()
    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
