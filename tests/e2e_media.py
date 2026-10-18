"""Media end-to-end test: local Prosody (with HTTP Upload) + mock OneBot v11 application.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_media.py`.
MODE=public uses accounts from .test_bots.json (BOT_JID / USER_JID) on real servers instead.

Covers:
  outgoing  base64:// and file:// images are uploaded (XEP-0363) and sent as URL body + OOB;
            text + image is split into two messages; delete_msg retracts both;
            in an OMEMO chat the image is encrypted (XEP-0454) and sent as an encrypted aesgcm:// link
  incoming  an HTTP Upload image (URL body + OOB) becomes an image segment;
            an encrypted aesgcm:// image is downloaded, decrypted and given as a file:// image segment
"""
import asyncio, base64, hashlib, io, json, logging, os, sys, time
from pathlib import Path
from urllib.parse import urlparse, unquote

import aiohttp
import slixmpp
from slixmpp.jid import JID
import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
from xmpp_gateway.__main__ import Gateway  # noqa: E402
from xmpp_gateway.xmpp import media, omemo as omemo_mod  # noqa: E402,F401

PORT = 6298
RUN = "tests/prosody/run"
MODE = os.environ.get("MODE", "local")
LOCAL = MODE == "local"
WORK = f"data/e2e_media_{MODE}"
UP = "http://127.0.0.1:15280/" if LOCAL else "https://"
results: dict[str, bool] = {}
# 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def check(name, cond, detail=""):
    results[name] = bool(cond)
    print(f"{'PASS' if cond else 'FAIL'} {name}" + (f"  ({detail})" if detail and not cond else ""), flush=True)


async def until(pred, timeout=20.0):
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
        return await asyncio.wait_for(fut, 60)


class User(slixmpp.ClientXMPP):
    def __init__(self, jid, pw):
        super().__init__(jid, pw)
        for x in ("xep_0030", "xep_0066", "xep_0359", "xep_0363", "xep_0380"):
            self.register_plugin(x)
        self.use_message_ids = True
        self.register_plugin("xep_0384", {"store_path": f"{WORK}/user.json", "btbv": True}, module=omemo_mod)
        self.inbox: list = []   # (stanza, decrypted body or None)
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)
        self.add_event_handler("message", self._msg)

    async def _start(self, _):
        self.send_presence()
        await self.get_roster()
        await self["xep_0384"].get_session_manager()
        self.ready.set()

    async def _msg(self, m):
        if m["type"] != "chat":
            return
        dec = None
        if self["xep_0384"].is_encrypted(m):
            try:
                d, _ = await self["xep_0384"].decrypt_message(m)
                dec = d["body"]
            except Exception as e:  # noqa: BLE001
                print("user decrypt failed:", repr(e), flush=True)
        self.inbox.append((m, dec))

    async def send_encrypted(self, to, text):
        msg = self.make_message(mto=to, mbody=text, mtype="chat")
        enc, _ = await self["xep_0384"].encrypt_message(msg, JID(to))
        enc["eme"]["namespace"] = "eu.siacs.conversations.axolotl"
        enc["eme"]["name"] = "OMEMO"
        enc.send()


async def fetch(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read() if r.status == 200 else None


async def main():
    logging.basicConfig(level=logging.WARNING)
    os.makedirs(WORK, exist_ok=True)
    Path(WORK, "gateway.db").unlink(missing_ok=True)
    if LOCAL:
        acc = json.load(open(f"{RUN}/accounts.json"))
        conn = {"server": "127.0.0.1", "port": 15222, "tls": "none"}
    else:
        bots = json.load(open(".test_bots.json"))
        pick = lambda env, i: next(a for a in bots if a["jid"] == os.environ[env]) if os.environ.get(env) else bots[i]
        acc, conn = {"bot": pick("BOT_JID", 0), "user": pick("USER_JID", 1)}, {}
    bot_jid, user_jid = acc["bot"]["jid"], acc["user"]["jid"]
    print(f"mode={MODE} bot={bot_jid} user={user_jid}", flush=True)
    cfg = {"xmpp": {**acc["bot"], **conn, "nick": "bot", "rooms": []},
           "omemo": {"enabled": True, "store_dir": f"{WORK}/omemo"},
           "media": {"dir": f"{WORK}/media", "aesgcm_insecure_http": LOCAL},
           "storage": {"db": f"{WORK}/gateway.db"},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())
    # gateway reads the OMEMO peer list at start: begin from a plaintext chat
    Path(WORK, "omemo", f"{bot_jid}.peers.json").unlink(missing_ok=True)

    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wa)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    gw = Gateway(path)
    await gw.start()
    u = User(user_jid, acc["user"]["password"])
    if LOCAL:
        u.enable_starttls = u.enable_direct_tls = False
        u.enable_plaintext = True
        u["feature_mechanisms"].unencrypted_plain = True
        u.connect(host="127.0.0.1", port=15222)
    else:
        u.connect()
    await asyncio.wait_for(u.ready.wait(), 40)
    await until(lambda: gw.xmpp.online and app.ws)
    u.send_presence_subscription(pto=bot_jid)
    await asyncio.sleep(2)

    # learn the user's OneBot id from a first plaintext message
    u.send_message(mto=bot_jid, mbody="hello", mtype="chat")
    ev = await until(lambda: [e for e in app.events if e["message"][0].get("data", {}).get("text") == "hello"], 40)
    uid = ev[-1]["user_id"]

    def new_msgs(start):  # ignore unrelated replies (e.g. another client of the bot account)
        return [x for x in u.inbox[start:] if x[0]["body"] and (x[0]["body"] == "look" or x[0]["body"].startswith(UP))]

    # --- outgoing base64 image, plaintext chat ---
    n = len(u.inbox)
    r = await app.call("send_private_msg", user_id=uid,
                       message=[{"type": "image", "data": {"file": "base64://" + base64.b64encode(PNG).decode()}}])
    got = await until(lambda: new_msgs(n))
    m = got[0][0] if got else None
    url = m["body"] if m else ""
    check("base64 image: action ok", r.get("status") == "ok", r)
    check("base64 image: body is upload URL with matching OOB",
          m is not None and url.startswith(UP) and m["oob"]["url"] == url, url)
    check("base64 image: uploaded bytes match", url and await fetch(url) == PNG)

    # --- text + file:// image -> two messages, delete_msg retracts both ---
    img = Path(WORK, "local.png").resolve()
    img.write_bytes(PNG)
    n = len(u.inbox)
    r = await app.call("send_private_msg", user_id=uid,
                       message=[{"type": "text", "data": {"text": "look"}},
                                {"type": "image", "data": {"file": img.as_uri()}}])
    got = await until(lambda: len(new_msgs(n)) >= 2 and new_msgs(n))
    bodies = [x[0]["body"] for x in (got or [])]
    check("text + file:// image: two messages, text first",
          len(bodies) >= 2 and bodies[0] == "look" and bodies[1].startswith(UP), bodies)
    n = len(u.inbox)
    await app.call("delete_msg", message_id=r["data"]["message_id"])
    retracts = await until(lambda: len([x for x in u.inbox[n:]
                                         if x[0].xml.find("{urn:xmpp:message-retract:1}retract") is not None]) >= 2)
    check("delete_msg retracts both messages", retracts)

    # --- missing file -> error, nothing sent ---
    r = await app.call("send_private_msg", user_id=uid, message=[{"type": "image", "data": {"file": "file:///nonexistent.png"}}])
    check("missing local file -> failed response", r.get("status") == "failed", r)

    # --- incoming HTTP Upload image ---
    up_url = await u["xep_0363"].upload_file("cat.png", size=len(PNG), content_type="image/png",
                                             input_file=io.BytesIO(PNG), timeout=20)
    k = len(app.events)
    msg = u.make_message(mto=bot_jid, mbody=up_url, mtype="chat")
    msg["oob"]["url"] = up_url
    msg.send()
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    seg = (ev or {}).get("message", [{}])[0]
    check("incoming upload -> image segment", seg.get("type") == "image" and seg["data"].get("url") == up_url, seg)

    # --- OMEMO chat: outgoing image is encrypted ---
    await u["xep_0384"].refresh_device_lists({JID(bot_jid)}, force_download=True)
    k = len(app.events)
    await u.send_encrypted(bot_jid, "secret hello")
    await until(lambda: len(app.events) > k)
    n = len(u.inbox)
    r = await app.call("send_private_msg", user_id=uid,
                       message=[{"type": "image", "data": {"file": "base64://" + base64.b64encode(PNG).decode()}}])
    got = await until(lambda: [x for x in u.inbox[n:] if x[1]])
    m, dec = got[0] if got else (None, "")
    check("OMEMO chat: image sent as encrypted aesgcm:// link",
          dec and dec.startswith("aesgcm://") and "aesgcm" not in m["body"]
          and m.xml.find("{jabber:x:oob}x") is None, dec)
    ok = False
    if dec:
        src, frag = media.aesgcm_split(dec, insecure_http=LOCAL)
        payload = await fetch(src)
        ok = payload is not None and payload != PNG and media.aesgcm_decrypt(payload, frag) == PNG
    check("OMEMO chat: server only stores ciphertext, decrypts to original", ok)

    # --- incoming encrypted aesgcm:// image ---
    payload, frag = media.aesgcm_encrypt(PNG)
    plain_url = await u["xep_0363"].upload_file("enc.png", size=len(payload), content_type="application/octet-stream",
                                                input_file=io.BytesIO(payload), timeout=20)
    k = len(app.events)
    await u.send_encrypted(bot_jid, media.aesgcm_url(plain_url, frag))
    ev = await until(lambda: len(app.events) > k and app.events[-1])
    seg = (ev or {}).get("message", [{}])[0]
    f = seg.get("data", {}).get("file", "")
    local = Path(unquote(urlparse(f).path)) if f.startswith("file://") else None
    check("incoming aesgcm image -> decrypted file:// image segment",
          seg.get("type") == "image" and local is not None and local.read_bytes() == PNG, seg)

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


asyncio.run(main())
