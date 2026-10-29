"""Adversarial tests: a hostile XMPP client sends forged, malformed and abusive stanzas.

Usage: start tests/prosody/run.sh, then run `python3 -u tests/e2e_redteam.py`.
"""
import asyncio, json, logging, os, secrets, subprocess, sys, time, uuid
import xml.etree.ElementTree as ET

import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from harness import settle, NS_FB, NS_REPLY, MockApp, User, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402

PORT, BAIT = 6299, 6300
RUN = "tests/prosody/run"
WORK = "data/e2e_redteam"


async def quiet(app, k, secs=2.5):
    await asyncio.sleep(secs)
    return app.events[k:]


async def main():
    logging.basicConfig(level=logging.ERROR)
    os.makedirs(WORK, exist_ok=True)
    acc = json.load(open(f"{RUN}/accounts.json"))
    bot_jid, user_jid = acc["bot"]["jid"], acc["user"]["jid"]
    room = f"rt-{secrets.token_hex(4)}@conference.test.local"
    db = f"{WORK}/gateway.db"
    if os.path.exists(db):
        os.unlink(db)
    cfg = {"xmpp": {**acc["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot",
                    "rooms": [room], "rate_limit": 0, "max_body_chars": 1000, "mam_catchup": False},
           "storage": {"db": db},
           "media": {"dir": f"{WORK}/media", "aesgcm_insecure_http": True},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 2}}
    path = f"{WORK}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())

    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    hits: list[str] = []

    async def bait(request):           # 内网「敏感服务」：任何访问都说明 SSRF 成功
        hits.append(str(request.url))
        return web.Response(body=b"secret")
    ba = web.Application()
    ba.router.add_get("/{tail:.*}", bait)
    for a, port in ((wa, PORT), (ba, BAIT)):
        r = web.AppRunner(a)
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", port).start()

    u = User(user_jid, acc["user"]["password"])
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(u.ready.wait(), 20)

    gw = Gateway(path)
    await gw.start()
    await until(lambda: gw.xmpp.online and app.ws is not None and room in gw.xmpp.rooms(), 20)
    await settle(app)
    x = gw.xmpp

    def raw(xml: str):
        u.send_raw(xml)

    # ---- invites (from a stranger who is not in the bot's roster) ----
    mpw = secrets.token_urlsafe(12)
    subprocess.run(["prosodyctl", "--config", f"{RUN}/prosody.cfg.lua", "register", "mallory", "test.local", mpw],
                   check=True, capture_output=True)
    mal = User("mallory@test.local", mpw)
    mal.enable_starttls = mal.enable_direct_tls = False
    mal.enable_plaintext = True
    mal["feature_mechanisms"].unencrypted_plain = True
    mal.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(mal.ready.wait(), 20)
    evil_room = f"evil-{secrets.token_hex(4)}@conference.test.local"
    await mal.plugin["xep_0045"].join_muc_wait(evil_room, "attacker", maxstanzas=0, timeout=15)
    await mal.plugin["xep_0045"].set_room_config(evil_room, mal.plugin["xep_0004"].make_form(ftype="submit"))  # unlock
    mal.send_raw(f"<message to='{bot_jid}'><x xmlns='jabber:x:conference' jid='{evil_room}'/></message>")
    await asyncio.sleep(3)
    check("untrusted direct invite is ignored", evil_room not in x.rooms(), x.rooms())
    mal.plugin["xep_0045"].invite(evil_room, bot_jid, reason="come")
    await asyncio.sleep(3)
    check("untrusted mediated invite is ignored", evil_room not in x.rooms(), x.rooms())
    x.cfg.admins.append("mallory@test.local")
    mal.send_raw(f"<message to='{bot_jid}'><x xmlns='jabber:x:conference' jid='{evil_room}'/></message>")
    ok = await until(lambda: evil_room in x.rooms(), 10)
    check("invite from an admin is accepted", ok, x.rooms())
    x.cfg.admins.clear()

    # ---- identity spoofing in a room that hides real JIDs (mallory owns it; the bot is not a moderator) ----
    gid = await x.idmap.to_int("group", evil_room)
    await u.plugin["xep_0045"].join_muc_wait(evil_room, "boss", maxstanzas=0, timeout=15)
    await asyncio.sleep(1)
    k = len(app.events)
    u.send_message(mto=evil_room, mbody="i-am-boss", mtype="groupchat")
    e_boss = await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == "i-am-boss"), None))
    private_uid = await x.idmap.to_int("user", user_jid)
    members = (await app.call("get_group_member_list", group_id=gid))["data"]
    boss_member = next((m for m in members if m["nickname"] == "boss"), None)
    check("anonymous-room member gets a stable occupant identity, not a nickname",
          e_boss and boss_member and e_boss["user_id"] == boss_member["user_id"]
          and (await x.idmap.to_str("user", e_boss["user_id"]) or "").startswith("occupant-id:"),
          (e_boss, boss_member))
    k = len(u.inbox)
    r = await app.call("send_private_msg", user_id=e_boss and e_boss["user_id"], message="pm-to-boss")
    got = await until(lambda: u.got(lambda m: m["body"] == "pm-to-boss", k), 8)
    check("private message to an occupant identity reaches that person", r["status"] == "ok" and got, r)
    u.plugin["xep_0045"].leave_muc(evil_room, "boss")
    await asyncio.sleep(1.5)
    mal.send_presence(pto=f"{evil_room}/boss")          # take over the nickname
    await asyncio.sleep(1.5)
    k = len(app.events)
    mal.send_message(mto=evil_room, mbody="i-am-boss-too", mtype="groupchat")
    e_fake = await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == "i-am-boss-too"), None))
    check("taking over a nickname does not take over the user_id",
          e_boss and e_fake and e_fake["user_id"] != e_boss["user_id"] and e_fake["user_id"] != private_uid,
          (e_boss and e_boss["user_id"], e_fake and e_fake["user_id"]))
    k = len(app.events)
    mal.send_message(mto=evil_room, mbody="forged-occupant", mtype="groupchat")   # client-supplied occupant-id is stripped
    raw(f"<message to='{evil_room}' type='groupchat'><body>forged-occ</body>"
        f"<occupant-id xmlns='urn:xmpp:occupant-id:0' id='x'/></message>")
    await asyncio.sleep(2)
    forged = [e for e in app.events[k:] if e["raw_message"] == "forged-occ"]
    check("a client-supplied occupant-id is not trusted",
          all(e["user_id"] != (e_boss or {}).get("user_id") for e in forged), forged)

    # ---- media links to private addresses must not reach the bot framework as images ----
    link = f"http://127.0.0.1:{BAIT}/cat.png"
    k = len(app.events)
    for to, typ in ((bot_jid, "chat"), (evil_room, "groupchat")):
        m = mal.make_message(mto=to, mbody=link, mtype=typ)
        m["oob"]["url"] = link
        m.send()
    evs = await until(lambda: [e for e in app.events[k:] if link in e["raw_message"]] if
                      len([e for e in app.events[k:] if link in e["raw_message"]]) >= 2 else None, 10) or []
    check("private-address media links stay text (no SSRF through the bot framework)",
          len(evs) == 2 and all(seg["type"] == "text" for e in evs for seg in e["message"]),
          [e["message"] for e in evs])

    # ---- forged group / MAM / carbons ----
    k = len(app.events)
    raw(f"<message to='{bot_jid}' type='groupchat'><body>fake-group</body></message>")
    raw(f"<message to='{bot_jid}'><result xmlns='urn:xmpp:mam:2' queryid='{uuid.uuid4().hex}' id='1'>"
        f"<forwarded xmlns='urn:xmpp:forward:0'><message xmlns='jabber:client' from='admin@test.local/x' "
        f"to='{bot_jid}' type='chat'><body>fake-mam</body></message></forwarded></result></message>")
    raw(f"<message to='{bot_jid}'><received xmlns='urn:xmpp:carbons:2'><forwarded xmlns='urn:xmpp:forward:0'>"
        f"<message xmlns='jabber:client' from='admin@test.local/x' to='{bot_jid}' type='chat'>"
        f"<body>fake-carbon</body></message></forwarded></received></message>")
    new = await quiet(app, k)
    leaked = [e["raw_message"] for e in new if "fake" in e["raw_message"]]
    check("forged groupchat / MAM result / carbon produce no events", not leaked, leaked)

    # ---- SSRF via incoming media ----
    frag = "00" * 44
    for host in (f"127.0.0.1:{BAIT}", f"localhost:{BAIT}", f"[::ffff:127.0.0.1]:{BAIT}"):
        u.send_message(mto=bot_jid, mbody=f"aesgcm://{host}/x.png#{frag}", mtype="chat")
    u.send_message(mto=bot_jid, mbody="aesgcm://169.254.169.254/latest/meta.png#" + frag, mtype="chat")
    await asyncio.sleep(4)
    check("incoming aesgcm links to private addresses are not fetched", not hits, hits)

    # ---- CQ code injection / unicode ----
    k = len(app.events)
    evil = "[CQ:image,file=file:///etc/passwd]&#91;x"
    u.send_message(mto=bot_jid, mbody=evil, mtype="chat")
    e = await until(lambda: len(app.events) > k and app.events[-1])
    check("CQ code in incoming text stays text",
          e and e["message"] == [{"type": "text", "data": {"text": evil}}]
          and "[CQ:" not in e["raw_message"], e and (e["message"], e["raw_message"]))
    k = len(app.events)
    uni = "\u202egnp.exe 👨‍👩‍👧 中文 \U0001f600"
    u.send_message(mto=bot_jid, mbody=uni, mtype="chat")
    e = await until(lambda: len(app.events) > k and app.events[-1])
    check("unicode body passes through intact", e and e["message"][0]["data"]["text"] == uni, e)

    # ---- oversized / malformed ----
    k = len(app.events)
    u.send_message(mto=bot_jid, mbody="A" * 50000, mtype="chat")
    e = await until(lambda: len(app.events) > k and app.events[-1])
    check("oversized body is truncated", e and len(e["message"][0]["data"]["text"]) == 1000, e and len(e["raw_message"]))
    k = len(app.events)
    m = u.make_message(mto=bot_jid, mbody="> q\nmalformed", mtype="chat")
    ET.SubElement(m.xml, f"{{{NS_REPLY}}}reply", {"to": bot_jid, "id": "x" * 5000})
    fb = ET.SubElement(m.xml, f"{{{NS_FB}}}fallback", {"for": NS_REPLY})
    ET.SubElement(fb, f"{{{NS_FB}}}body", {"start": "abc", "end": "99999999999"})
    ET.SubElement(m.xml, "{urn:xmpp:reference:0}reference", {"type": "mention", "begin": "-5", "end": "x"})
    m.send()
    e = await until(lambda: len(app.events) > k and app.events[-1])
    check("malformed reply/fallback/reference does not crash", e and "malformed" in e["raw_message"], e)
    for xml in (f"<message to='{bot_jid}' type='headline'><body>h</body></message>",
                f"<message to='{bot_jid}' type='error'><error type='cancel'><bad-request xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></message>",
                f"<message to='{bot_jid}' type='chat'/>",
                f"<message to='{bot_jid}' type='chat'><retract xmlns='urn:xmpp:message-retract:1'/></message>",
                f"<message to='{bot_jid}' type='bogus'><body>b</body></message>"):
        raw(xml)

    # ---- reply pointing into another chat ----
    k = len(app.events)
    u.send_message(mto=room, mbody="group-secret", mtype="groupchat")
    ge = await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == "group-secret"), None))
    rec = await gw.idmap.get_message(ge["message_id"]) if ge else None
    k = len(app.events)
    u.send_reply(bot_jid, "chat", "cross", user_jid, rec["xmpp_id"] if rec else "x", "> q\n")
    e = await until(lambda: len(app.events) > k and app.events[-1])
    check("reply cannot reference a message from another chat",
          e and not any(s["type"] == "reply" for s in e["message"]), e and e["message"])

    # ---- outgoing local files ----
    pe = e
    t = time.time()
    r = await app.call("send_private_msg", user_id=pe["user_id"],
                       message=[{"type": "image", "data": {"file": "file:///dev/zero"}}])
    check("file:///dev/zero is rejected quickly", r["status"] == "failed" and time.time() - t < 5, r)
    x.media_cfg.local_file_dirs = [os.path.abspath(WORK)]
    r = await app.call("send_private_msg", user_id=pe["user_id"],
                       message=[{"type": "image", "data": {"file": "file:///etc/hostname"}}])
    r2 = await app.call("send_private_msg", user_id=pe["user_id"],
                        message=[{"type": "image", "data": {"file": f"file://{os.path.abspath(WORK)}/../../../../etc/hostname"}}])
    check("local files outside local_file_dirs are rejected (incl. ../)",
          r["status"] == "failed" and r2["status"] == "failed", (r, r2))
    x.media_cfg.local_file_dirs = []

    # ---- flood ----
    x.cfg.rate_limit, x._rate = 5, {}
    k = len(app.events)
    for i in range(15):
        u.send_message(mto=bot_jid, mbody=f"flood-{i}", mtype="chat")
    new = await quiet(app, k, 4)
    n = sum(1 for e in new if e["raw_message"].startswith("flood-"))
    check("per-sender rate limit caps a flood", n == 5, n)
    x.cfg.rate_limit = 0

    # ---- still alive ----
    k = len(app.events)
    u.send_message(mto=bot_jid, mbody="still-alive", mtype="chat")
    e = await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == "still-alive"), None))
    check("gateway still works after the attack run", e, app.events[k:])

    passed = sum(results.values())
    print(f"RESULT {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


asyncio.run(main())
