"""OMEMO configuration branches, end to end (mock OneBot app, local Prosody):

  trust = "manual"       a device the bot has not trusted gets no reply at all (never a plaintext fallback)
  resync_notice = ""     a broken session is rebuilt silently, and the next message gets through
  resync_cooldown        two undecryptable messages in a row: one notice with the default 300 s, two with 0

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_config_omemo.py`.
"""
import asyncio, json, logging, os, shutil, sys

import tomli_w
from aiohttp import web
from slixmpp.jid import JID

sys.path[:0] = ["src", "tests"]
import e2e_omemo_resync as base  # noqa: E402
from harness import MockApp, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT = 6421
RUN = "tests/prosody/run"
WORK = "data/e2e_config_omemo"
base.WORK = WORK                     # OmemoUser keeps its store under base.WORK
NS_OLD = base.NS_OLD
ACC = json.load(open(f"{RUN}/accounts.json"))
BOT, USER = ACC["bot"]["jid"], ACC["user"]["jid"]
LOG = base.Counter()


def write_cfg(name, omemo):
    cfg = {"xmpp": {**ACC["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot",
                    "rooms": [], "rate_limit": 0, "mam_catchup": False},
           "omemo": {"enabled": True, "store_dir": f"{WORK}/{name}/bot", **omemo},
           "storage": {"db": f"{WORK}/{name}/gateway.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 1}}
    os.makedirs(f"{WORK}/{name}", exist_ok=True)
    path = f"{WORK}/{name}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())
    return path


async def start(path, app):
    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws, 20)
    await asyncio.sleep(2)
    return gw


async def new_user(name):
    """A fresh OMEMO device for the test user (own store per case)."""
    base.WORK = f"{WORK}/{name}"
    u = base.OmemoUser(USER, ACC["user"]["password"], BOT)
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u.plugin["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(u.ready.wait(), 20)
    u.send_presence_subscription(pto=BOT)
    await asyncio.sleep(1)
    await u.plugin["xep_0384"].refresh_device_lists({JID(BOT)}, force_download=True)
    return u


def drop_sessions(name):
    """Make the bot lose its sessions with the user (as if an old backup had been restored)."""
    path = f"{WORK}/{name}/bot/{BOT}.json"
    data = json.load(open(path))
    keys = [k for k in data if k.startswith(f"/{NS_OLD}/{USER}/")]
    for k in keys:
        del data[k]
    json.dump(data, open(path, "w"))
    return keys


async def first_contact(u, app, text):
    k = len(app.events)
    (await u.encrypted(text)).send()
    return await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == text), None), 20)


async def case_manual(app):
    path = write_cfg("manual", {"trust": "manual"})
    gw = await start(path, app)
    u = await new_user("manual")
    n_log, n_in = len(LOG.lines), len(u.inbox)
    ev = await first_contact(u, app, "manual-1")
    if ev:
        await app.call("send_private_msg", user_id=ev["user_id"], message="manual-reply")
    await asyncio.sleep(4)
    check("trust=manual: the untrusted device gets no encrypted reply",
          not any(m == ("enc", "manual-reply") for m in u.inbox[n_in:]), u.inbox[n_in:])
    check("trust=manual: and no plaintext fallback either",
          not any(m[0] == "plain" for m in u.inbox[n_in:]), u.inbox[n_in:])
    check("trust=manual: the skipped device is logged", LOG.count("is not trusted (trust=manual)") > 0 or
          any("not trusted" in x for x in LOG.lines[n_log:]), LOG.lines[n_log:][-5:])
    u.disconnect()
    await gw.stop()


async def case_silent_resync(app):
    path = write_cfg("silent", {"trust": "btbv", "resync_notice": ""})
    gw = await start(path, app)
    u = await new_user("silent")
    ok = await first_contact(u, app, "silent-1")
    check("resync_notice=\"\": test setup, first contact works", ok)
    await gw.stop()
    check("resync_notice=\"\": test setup, bot sessions dropped", drop_sessions("silent"))
    gw = await start(path, app)
    k, n_in = len(app.events), len(u.inbox)
    (await u.encrypted("silent-lost")).send()
    await asyncio.sleep(4)
    check("resync_notice=\"\": no notice is sent to the user", u.inbox[n_in:] == [], u.inbox[n_in:])
    check("resync_notice=\"\": the undecryptable message is not forwarded",
          "silent-lost" not in [e["raw_message"] for e in app.events[k:]])
    ev = await first_contact(u, app, "silent-again")
    check("resync_notice=\"\": the session is still rebuilt, the next message gets through", ev)
    u.disconnect()
    await gw.stop()


async def case_cooldown(app, cooldown):
    name = f"cooldown-{cooldown}"
    path = write_cfg(name, {"trust": "btbv", **({"resync_cooldown": cooldown} if cooldown is not None else {})})
    gw = await start(path, app)
    u = await new_user(name)
    await first_contact(u, app, f"{name}-1")
    await gw.stop()
    drop_sessions(name)
    gw = await start(path, app)
    n_in = len(u.inbox)
    a, b = await u.encrypted(f"{name}-lost-a"), await u.encrypted(f"{name}-lost-b")   # both on the old session
    a.send()
    b.send()
    await asyncio.sleep(6)
    notices = [m for m in u.inbox[n_in:] if m[0] == "enc"]
    want = 1 if cooldown is None else 2
    label = "default 300 s" if cooldown is None else f"{cooldown} s"
    check(f"resync_cooldown={label}: {want} notice(s) for two undecryptable messages in a row",
          len(notices) == want, u.inbox[n_in:])
    u.disconnect()
    await gw.stop()


async def main():
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("xmpp_gateway").addHandler(LOG)
    logging.getLogger("xmpp_gateway").setLevel(logging.INFO)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    r = web.AppRunner(wa)
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", PORT).start()
    for case in (case_manual(app), case_silent_resync(app), case_cooldown(app, None), case_cooldown(app, 0)):
        try:
            await case
        except Exception as e:  # noqa: BLE001
            check(f"{case.__name__} ran without errors", False, repr(e))
            logging.getLogger("e2e").exception("case failed")
    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
