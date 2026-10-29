"""Red-team test of the whole chain: hostile XMPP user -> gateway -> a real bot framework.

The bot framework's admin is user@test.local; the attacker is mallory@test.local.
Checked per framework:
  * admin-only command from the attacker is refused (direct chat, a room where the bot sees real JIDs,
    and a room that hides real JIDs after the attacker takes over the admin's nickname);
  * the same command from the admin works (where the framework has admin-only commands);
  * a media link to a private address is not fetched by anyone (SSRF canary);
  * CQ-code text is not turned into a real mention or image;
  * a flood from the attacker is capped and the chain still answers afterwards.

Usage:
  1. tests/prosody/run.sh is running.
  2. python3 tests/e2e_redteam_chain.py --admin-id   -> prints the admin's OneBot user_id.
  3. Configure the framework with that user_id as admin (AstrBot admins_id, NoneBot SUPERUSERS) and start it.
  4. FRAMEWORK=astrbot|nonebot|langbot FRAMEWORK_WS=ws://... python3 -u tests/e2e_redteam_chain.py

Per-framework commands (env overrides: ADMIN_CMD, OPEN_CMD, DENIED, ALLOWED, GROUP_PREFIX):
  astrbot: admin "/provider", open "/help"; nonebot: admin "/sudo", open "/help" (tests/nonebot_bot.py);
  langbot: no admin-only commands are built in, so only the open command "!help" (mention needed in groups).
"""
import asyncio, json, logging, os, secrets, subprocess, sys

import tomli_w
from aiohttp import web

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from harness import User, check, results, until  # noqa: E402
from xmpp_gateway.__main__ import Gateway  # noqa: E402
from xmpp_gateway.core.idmap import IdMap  # noqa: E402

RUN = "tests/prosody/run"
WORK = "data/e2e_redteam_chain"
DB = f"{WORK}/gateway.db"
BAIT = 6301
FW = os.environ.get("FRAMEWORK", "astrbot")
PRESETS = {
    "astrbot": dict(ws="ws://127.0.0.1:6199/ws", admin="/provider", open="/help", prefix="",
                    denied="权限不足", allowed=""),
    "nonebot": dict(ws="ws://127.0.0.1:8080/onebot/v11/ws", admin="/sudo", open="/help", prefix="",
                    denied="", allowed="sudo ok"),
    "langbot": dict(ws="ws://127.0.0.1:2280/ws", admin="", open="!help", prefix="bot: ", denied="", allowed=""),
}
P = PRESETS[FW]
WS = os.environ.get("FRAMEWORK_WS", P["ws"])
ADMIN_CMD = os.environ.get("ADMIN_CMD", P["admin"])
OPEN_CMD = os.environ.get("OPEN_CMD", P["open"])
PREFIX = os.environ.get("GROUP_PREFIX", P["prefix"])
DENIED = os.environ.get("DENIED", P["denied"])     # text a refusal contains ("" = the framework stays silent)
ALLOWED = os.environ.get("ALLOWED", P["allowed"])  # text a granted admin command contains ("" = any reply but DENIED)


def acc():
    return json.load(open(f"{RUN}/accounts.json"))


async def admin_id() -> int:
    os.makedirs(WORK, exist_ok=True)
    m = IdMap(DB)
    await m.open()
    n = await m.to_int("user", acc()["user"]["jid"])
    await m.close()
    return n


def connect(u: User):
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)


def from_bot(bot_jid, room=None):
    def pred(m):
        if room:
            return m["type"] == "groupchat" and m["from"].bare == room and m["mucnick"] == "bot" and m["body"]
        return m["type"] == "chat" and m["from"].bare == bot_jid and m["body"]
    return pred


async def ask(u: User, to: str, body: str, pred, wait=12.0):
    """Send body, return the bot's replies that arrive within wait seconds (stops early on the first one)."""
    k = len(u.inbox)
    u.send_message(mto=to, mbody=body, mtype="groupchat" if "@conference." in to else "chat")
    await until(lambda: u.got(pred, k), wait)
    await asyncio.sleep(1.5)
    return [m["body"] for m in u.inbox[k:] if pred(m)]


def refused(replies):
    return not replies or (DENIED and all(DENIED in r for r in replies))


def granted(replies):
    return bool(replies) and (ALLOWED and any(ALLOWED in r for r in replies)
                              or not ALLOWED and not any(DENIED and DENIED in r for r in replies))


async def main():
    logging.basicConfig(level=logging.ERROR)
    a = acc()
    bot_jid, user_jid = a["bot"]["jid"], a["user"]["jid"]
    uid = await admin_id()
    room = f"chain-{secrets.token_hex(4)}@conference.test.local"
    cfg = {"xmpp": {**a["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "nick": "bot",
                    "rooms": [room], "mam_catchup": False, "rate_limit": 0},
           "storage": {"db": DB},
           "media": {"dir": f"{WORK}/media"},
           "onebot_v11": {"enabled": True, "reverse_ws": [WS], "reconnect_max": 2}}
    path = f"{WORK}/gateway.toml"
    open(path, "wb").write(tomli_w.dumps(cfg).encode())

    hits: list[str] = []

    async def bait(request):
        hits.append(f"{request.remote} {request.url}")
        return web.Response(body=b"\x89PNG\r\n\x1a\nsecret", content_type="image/png")
    ba = web.Application()
    ba.router.add_get("/{tail:.*}", bait)
    r = web.AppRunner(ba)
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", BAIT).start()

    gw = Gateway(path)
    await gw.start()
    x = gw.xmpp
    await until(lambda: x.online and room in x.rooms(), 25)

    u = User(user_jid, a["user"]["password"])
    connect(u)
    mpw = secrets.token_urlsafe(12)
    subprocess.run(["prosodyctl", "--config", f"{RUN}/prosody.cfg.lua", "register", "mallory", "test.local", mpw],
                   check=True, capture_output=True)
    mal = User("mallory@test.local", mpw)
    connect(mal)
    await asyncio.wait_for(u.ready.wait(), 20)
    await asyncio.wait_for(mal.ready.wait(), 20)
    await asyncio.sleep(3)
    print(f"framework={FW} admin user_id={uid}", flush=True)

    ok_reply = await ask(u, bot_jid, OPEN_CMD, from_bot(bot_jid), 20)
    check("chain is up (open command answered)", ok_reply, ok_reply)

    # ---- privilege: direct chat ----
    if ADMIN_CMD:
        rep = await ask(mal, bot_jid, ADMIN_CMD, from_bot(bot_jid))
        check("attacker's admin command refused (direct)", refused(rep), rep)
        rep = await ask(u, bot_jid, ADMIN_CMD, from_bot(bot_jid))
        check("admin's admin command works (direct)", granted(rep), rep)

    # ---- privilege: room where the bot sees real JIDs; attacker uses the admin's nickname ----
    await u.plugin["xep_0045"].join_muc_wait(room, "tester", maxstanzas=0, timeout=15)
    await mal.plugin["xep_0045"].join_muc_wait(room, "user", maxstanzas=0, timeout=15)
    await asyncio.sleep(2)
    gpred = from_bot(bot_jid, room)
    if ADMIN_CMD:
        rep = await ask(mal, room, PREFIX + ADMIN_CMD, gpred)
        check("attacker's admin command refused (group, admin's nickname)", refused(rep), rep)
        rep = await ask(u, room, PREFIX + ADMIN_CMD, gpred)
        check("admin's admin command works (group)", granted(rep), rep)

    # ---- privilege: room that hides real JIDs; attacker takes over the nickname the admin used ----
    anon = f"anon-{secrets.token_hex(4)}@conference.test.local"
    await mal.plugin["xep_0045"].join_muc_wait(anon, "owner", maxstanzas=0, timeout=15)
    await mal.plugin["xep_0045"].set_room_config(anon, mal.plugin["xep_0004"].make_form(ftype="submit"))
    x.cfg.admins.append("mallory@test.local")          # let the bot accept the attacker's invite
    mal.send_raw(f"<message to='{bot_jid}'><x xmlns='jabber:x:conference' jid='{anon}'/></message>")
    await until(lambda: anon in x.rooms(), 15)
    x.cfg.admins.clear()
    await u.plugin["xep_0045"].join_muc_wait(anon, "boss", maxstanzas=0, timeout=15)
    await asyncio.sleep(1)
    apred = from_bot(bot_jid, anon)
    rep_admin = await ask(u, anon, PREFIX + OPEN_CMD, apred)
    u.plugin["xep_0045"].leave_muc(anon, "boss")
    await asyncio.sleep(1.5)
    mal.send_presence(pto=f"{anon}/boss")
    await asyncio.sleep(2)
    if ADMIN_CMD:
        rep = await ask(mal, anon, PREFIX + ADMIN_CMD, apred)
        check("attacker's admin command refused (anonymous room, taken-over nickname)", refused(rep), rep)
    check("anonymous room still works for ordinary commands", rep_admin, rep_admin)

    # ---- SSRF canary: media links to private addresses ----
    link = f"http://127.0.0.1:{BAIT}/cat.png"
    for to, typ in ((bot_jid, "chat"), (room, "groupchat")):
        m = mal.make_message(mto=to, mbody=link, mtype=typ)
        m["oob"]["url"] = link
        m.send()
    await asyncio.sleep(10)
    check("private-address media link fetched by nobody", not hits, hits)

    # ---- CQ-code injection: must not become a real @all / image ----
    k = len(u.inbox)
    mal.send_message(mto=room, mbody=f"[CQ:at,qq=all][CQ:image,file=file:///etc/passwd] {PREFIX}{OPEN_CMD}",
                     mtype="groupchat")
    await asyncio.sleep(6)
    leaked = [m["body"] for m in u.inbox[k:] if gpred(m) and ("root:" in m["body"])]
    check("CQ text injection leaks nothing", not leaked, leaked)

    # ---- flood ----
    x.cfg.rate_limit, x._rate = 5, {}
    k = len(mal.inbox)
    for i in range(30):
        mal.send_message(mto=bot_jid, mbody=OPEN_CMD, mtype="chat")
    await asyncio.sleep(15)
    n = len([m for m in mal.inbox[k:] if from_bot(bot_jid)(m)])
    check("attacker's flood is capped by the gateway", n <= 5, n)
    x.cfg.rate_limit = 0
    rep = await ask(u, bot_jid, OPEN_CMD, from_bot(bot_jid), 20)
    check("chain still answers after the attack run", rep, rep)

    passed = sum(results.values())
    print(f"RESULT {FW} {passed}/{len(results)} passed", flush=True)
    os._exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    if "--admin-id" in sys.argv:
        print(asyncio.run(admin_id()))
    else:
        asyncio.run(main())
