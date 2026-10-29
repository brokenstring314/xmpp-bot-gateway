"""Fault injection and load: bot app outage, XMPP server restart, bursts in both directions.

Usage: start tests/prosody/run.sh (with setsid), then run `python3 -u tests/e2e_chaos.py`.
The test restarts Prosody itself.
"""
import asyncio, json, logging, os, resource, secrets, signal, subprocess, sys, time

import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from harness import settle, MockApp, User, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6301
RUN = "tests/prosody/run"
WORK = "data/e2e_chaos"


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def new_user(acc):
    u = User(acc["user"]["jid"], acc["user"]["password"])
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    return u


def restart_prosody():
    pid = int(open(f"{RUN}/prosody.pid").read().strip())
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try:
            os.kill(pid, 0)
            time.sleep(0.2)
        except ProcessLookupError:
            break
    subprocess.Popen(["setsid", "bash", "tests/prosody/run.sh"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


async def main():
    logging.basicConfig(level=logging.ERROR)
    os.makedirs(WORK, exist_ok=True)
    acc = json.load(open(f"{RUN}/accounts.json"))
    bot_jid = acc["bot"]["jid"]
    room = f"chaos-{secrets.token_hex(4)}@conference.test.local"
    db = f"{WORK}/gateway.db"
    if os.path.exists(db):
        os.unlink(db)
    cfg = {"xmpp": {**acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot",
                    "rooms": [room], "rate_limit": 0},
           "storage": {"db": db},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())

    app = MockApp()

    async def serve():
        wa = web.Application()
        wa.router.add_get("/ws", app.handler)
        r = web.AppRunner(wa)
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", PORT).start()
        return r
    runner = await serve()
    u = new_user(acc)
    await asyncio.wait_for(u.ready.wait(), 20)
    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws is not None and room in gw.xmpp.rooms(), 20)
    await settle(app)
    await u.plugin["xep_0045"].join_muc_wait(room, "alice", maxstanzas=0, timeout=15)

    # 1. bot app outage: events queue up and arrive in order after it comes back
    await runner.cleanup()
    app.ws = None
    await asyncio.sleep(1)
    for i in range(5):
        u.send_message(mto=bot_jid, mbody=f"queued-{i}", mtype="chat")
    await asyncio.sleep(3)
    k = len(app.events)
    runner = await serve()
    got = await until(lambda: [e["raw_message"] for e in app.events[k:] if e["raw_message"].startswith("queued-")]
                      if sum(e["raw_message"].startswith("queued-") for e in app.events[k:]) >= 5 else None, 20)
    check("events during app outage are delivered in order", got == [f"queued-{i}" for i in range(5)], got)

    # 2. XMPP server restart: gateway reconnects and rejoins the room
    restart_prosody()
    await until(lambda: not gw.xmpp.online, 15)
    ok = await until(lambda: gw.xmpp.online and room in gw.xmpp.rooms(), 60)
    check("gateway reconnects and rejoins after server restart", ok)
    u.disconnect()
    u = new_user(acc)
    await asyncio.wait_for(u.ready.wait(), 30)
    await u.plugin["xep_0045"].join_muc_wait(room, "alice", maxstanzas=0, timeout=15)
    k = len(app.events)
    u.send_message(mto=room, mbody="after-restart", mtype="groupchat")
    u.send_message(mto=bot_jid, mbody="after-restart-pm", mtype="chat")
    got = await until(lambda: {"after-restart", "after-restart-pm"} <= {e["raw_message"] for e in app.events[k:]}, 20)
    check("group and private messages work after server restart", got, [e["raw_message"] for e in app.events[k:]])

    # 3. inbound burst
    n = 300
    rss0 = rss_mb()
    k = len(app.events)
    t = time.time()
    for i in range(n):
        u.send_message(mto=bot_jid, mbody=f"burst-{i}", mtype="chat")
    await until(lambda: sum(e["raw_message"].startswith("burst-") for e in app.events[k:]) >= n, 90)
    got = [e["raw_message"] for e in app.events[k:] if e["raw_message"].startswith("burst-")]
    dt = time.time() - t
    check(f"inbound burst of {n}: all delivered in order", got == [f"burst-{i}" for i in range(n)],
          f"{len(got)} delivered")
    print(f"  inbound {n} msgs in {dt:.1f}s ({n / dt:.0f}/s)", flush=True)

    # 4. outbound burst through the API
    uid = app.events[-1]["user_id"]
    k = len(u.inbox)
    t = time.time()
    rs = await asyncio.gather(*(app.call("send_private_msg", user_id=uid, message=f"out-{i}") for i in range(200)))
    await until(lambda: sum(str(m["body"]).startswith("out-") for m in u.inbox[k:]) >= 200, 60)
    got = [m["body"] for m in u.inbox[k:] if str(m["body"]).startswith("out-")]
    check("outbound burst of 200: all ok and received",
          all(r["status"] == "ok" for r in rs) and sorted(got) == sorted(f"out-{i}" for i in range(200)), len(got))
    print(f"  outbound 200 msgs in {time.time() - t:.1f}s; peak RSS {rss0:.0f} -> {rss_mb():.0f} MB", flush=True)

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


asyncio.run(main())
