"""Long-running soak test against real XMPP servers.

Runs the gateway as a child process plus, in this process, a mock OneBot v11 app that echoes every
private message and a scripted XMPP user that pings the bot at a fixed interval (alternating plain
and OMEMO). Writes, into the output directory:

  pings.csv    one row per ping: time, kind, result (ok/lost), round-trip seconds
  samples.csv  every few minutes: gateway RSS, open files, database size, reconnect counts
  gateway.log  the gateway's own log
  report.md    summary, rewritten after every sample (so it is useful even if the run is cut short)

Usage (Linux, WSL or macOS):
  python3 tests/soak.py -c soak.toml --hours 24
  soak.toml:
    [bot]
    jid = "bot@example.org"
    password = "..."
    [user]
    jid = "tester@example.net"
    password = "..."
Stop early with Ctrl-C; report.md is written either way.
"""
import argparse, asyncio, csv, json, os, re, signal, statistics, subprocess, sys, time, tomllib
from collections import Counter
from pathlib import Path

import slixmpp
import tomli_w
from aiohttp import web
from slixmpp.jid import JID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from xmpp_gateway.xmpp import omemo as omemo_mod  # noqa: E402

PORT = 6290
NS_OLD = "eu.siacs.conversations.axolotl"


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------- mock OneBot v11 app: echoes every private message ----------
class EchoApp:
    def __init__(self):
        self.connects = 0
        self.received = 0
        self.ws = None
        self.n = 0

    async def handler(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.ws, self.connects = ws, self.connects + 1
        async for m in ws:
            try:
                d = json.loads(m.data)
            except ValueError:
                continue
            if d.get("post_type") == "message" and d.get("message_type") == "private":
                self.received += 1
                self.n += 1
                await ws.send_str(json.dumps({"action": "send_private_msg", "echo": f"s{self.n}", "params": {
                    "user_id": d["user_id"], "message": "echo:" + d.get("raw_message", "")}}))
        return ws


# ---------- scripted user ----------
class Tester(slixmpp.ClientXMPP):
    def __init__(self, jid, pw, bot_jid, store):
        super().__init__(jid, pw)
        self.bot_jid = bot_jid
        self.use_message_ids = True
        self.register_plugin("xep_0199", {"keepalive": True, "interval": 120})
        self.register_plugin("xep_0380")
        self.register_plugin("xep_0384", {"store_path": store, "btbv": True}, module=omemo_mod)
        self.waiting: dict[str, asyncio.Future] = {}
        self.online = asyncio.Event()
        self.sessions = 0
        self.stopping = False
        self.conn_args: dict = {}
        self.add_event_handler("session_start", self._start)
        self.add_event_handler("disconnected", self._disconnected)
        self.add_event_handler("message", self._on_msg)

    async def _start(self, _):
        self.sessions += 1
        self.send_presence()
        await self.get_roster()
        await self.plugin["xep_0384"].get_session_manager()
        self.online.set()

    async def _disconnected(self, _):
        self.online.clear()
        if not self.stopping:
            await asyncio.sleep(10)
            self.connect(**self.conn_args)

    async def _on_msg(self, m):
        if m["type"] != "chat" or m["from"].bare != self.bot_jid:
            return
        body = m["body"]
        if self.plugin["xep_0384"].is_encrypted(m):
            try:
                dec, _ = await self.plugin["xep_0384"].decrypt_message(m)
                body = dec["body"] or ""
            except Exception as e:  # noqa: BLE001
                print(now(), "tester: decrypt failed:", repr(e), flush=True)
                return
        hit = re.match(r"echo:(ping-\d+)", body or "")
        if hit and hit.group(1) in self.waiting and not self.waiting[hit.group(1)].done():
            self.waiting[hit.group(1)].set_result(time.monotonic())

    async def ping(self, tag: str, encrypted: bool):
        msg = self.make_message(mto=self.bot_jid, mbody=tag, mtype="chat")
        if encrypted:
            msg, errors = await self.plugin["xep_0384"].encrypt_message(msg, JID(self.bot_jid))
            msg["eme"]["namespace"], msg["eme"]["name"] = NS_OLD, "OMEMO"
        msg.send()


# ---------- process metrics ----------
def proc_stats(pid: int) -> tuple[float, int]:
    """(RSS MB, open files) of the gateway process."""
    try:
        import psutil  # optional, needed on macOS
        p = psutil.Process(pid)
        return p.memory_info().rss / 2**20, len(p.open_files()) + len(p.net_connections())
    except ImportError:
        pass
    rss = 0.0
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) / 1024
    return rss, len(os.listdir(f"/proc/{pid}/fd"))


def size_mb(*paths) -> float:
    return sum(os.path.getsize(p) for p in paths if os.path.exists(p)) / 2**20


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def write_report(out: Path, started: float, pings: list, samples: list, log_path: Path, app, tester):
    ok = [p for p in pings if p[2] == "ok"]
    lat = [p[3] for p in ok]
    lost = [p for p in pings if p[2] != "ok"]
    levels, top = Counter(), Counter()
    sessions = 0
    try:
        for line in open(log_path, encoding="utf8", errors="replace"):
            if "XMPP session started" in line:
                sessions += 1
            m = re.match(r"\S+ \S+ (WARNING|ERROR|CRITICAL) (\S+): (.*)", line)
            if m:
                levels[m.group(1)] += 1
                top[re.sub(r"\d+", "N", m.group(3))[:110]] += 1
    except FileNotFoundError:
        pass
    rss = [s[1] for s in samples]
    hours = (time.time() - started) / 3600
    lines = [
        "# Soak report", "",
        f"- Started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}, ran {hours:.2f} h",
        f"- Pings: {len(pings)} sent, {len(ok)} answered, {len(lost)} lost"
        + (f" ({100 * len(lost) / len(pings):.2f}%)" if pings else ""),
        f"- Round trip: median {statistics.median(lat):.2f}s, p95 {pct(lat, .95):.2f}s, max {max(lat):.2f}s"
        if lat else "- Round trip: no answers",
        f"- Gateway RSS: start {rss[0]:.1f} MB, end {rss[-1]:.1f} MB, max {max(rss):.1f} MB" if rss else "",
        f"- Open files: start {samples[0][2]}, end {samples[-1][2]}" if samples else "",
        f"- Database + WAL: end {samples[-1][3]:.2f} MB" if samples else "",
        f"- Gateway XMPP sessions: {sessions} (1 = never reconnected); bot framework connections: {app.connects}",
        f"- Tester XMPP sessions: {tester.sessions}",
        f"- Gateway log: {levels['WARNING']} warnings, {levels['ERROR'] + levels['CRITICAL']} errors", "",
    ]
    if lost:
        lines += ["## Lost pings (first 20)", ""] + [f"- {p[0]} {p[1]}" for p in lost[:20]] + [""]
    if top:
        lines += ["## Most frequent warnings / errors", ""] + [f"- {n}x {msg}" for msg, n in top.most_common(15)]
    (out / "report.md").write_text("\n".join(x for x in lines if x is not None) + "\n", encoding="utf8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="soak.toml")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--interval", type=float, default=60, help="seconds between pings")
    ap.add_argument("--sample", type=float, default=300, help="seconds between resource samples")
    ap.add_argument("--timeout", type=float, default=90, help="a ping without echo after this is lost")
    ap.add_argument("--data", default="data/soak", help="persistent gateway data (db, OMEMO stores)")
    args = ap.parse_args()
    acc = tomllib.load(open(args.config, "rb"))
    bot, user = acc["bot"], acc["user"]
    out = Path(f"soak-{time.strftime('%Y%m%d-%H%M%S')}")
    out.mkdir()
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    cfg = {"xmpp": {"jid": bot["jid"], "password": bot["password"], "nick": "soakbot", "rooms": [],
                    "rate_limit": 0, **{k: bot[k] for k in ("server", "port", "tls") if k in bot}},
           "omemo": {"enabled": True, "trust": "btbv", "store_dir": str(data / "omemo")},
           "storage": {"db": str(data / "gateway.db")},
           "media": {"dir": str(data / "media")},
           "onebot_v11": {"enabled": True, "reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"]}}
    gw_cfg = data / "gateway.toml"
    gw_cfg.write_bytes(tomli_w.dumps(cfg).encode())

    app = EchoApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wa)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    log_path = out / "gateway.log"
    logf = open(log_path, "ab")
    gw = subprocess.Popen([sys.executable, "-m", "xmpp_gateway", "-c", str(gw_cfg)], stdout=logf,
                          stderr=subprocess.STDOUT,
                          env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")})
    print(now(), f"gateway pid {gw.pid}, output in {out}/", flush=True)

    tester = Tester(user["jid"], user["password"], bot["jid"], str(data / "tester-omemo.json"))
    if user.get("tls") == "none":   # local test server only
        tester.enable_starttls = tester.enable_direct_tls = False
        tester.enable_plaintext = True
        tester.plugin["feature_mechanisms"].unencrypted_plain = True
    tester.conn_args = {"host": user["server"], "port": user.get("port", 5222)} if "server" in user else {}
    tester.connect(**tester.conn_args)
    await asyncio.wait_for(tester.online.wait(), 90)
    tester.send_presence_subscription(pto=bot["jid"])
    for _ in range(60):
        if app.ws is not None:
            break
        await asyncio.sleep(1)
    await asyncio.sleep(10)
    await tester.plugin["xep_0384"].refresh_device_lists({JID(bot["jid"])}, force_download=True)

    pings: list = []
    samples: list = []
    started = time.time()
    end = started + args.hours * 3600
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    pf = open(out / "pings.csv", "w", newline="")
    pw = csv.writer(pf)
    pw.writerow(["time", "tag", "kind", "result", "rtt_s"])
    sf = open(out / "samples.csv", "w", newline="")
    sw = csv.writer(sf)
    sw.writerow(["time", "rss_mb", "open_files", "db_mb", "media_mb", "app_connects", "tester_sessions"])

    async def one_ping(n: int):
        tag, kind = f"ping-{n}", ("omemo" if n % 2 else "plain")
        fut = loop.create_future()
        tester.waiting[tag] = fut
        t0 = time.monotonic()
        try:
            if not tester.online.is_set():
                raise ConnectionError("tester offline")
            await tester.ping(tag, kind == "omemo")
            t1 = await asyncio.wait_for(fut, args.timeout)
            row = (now(), tag, "ok", round(t1 - t0, 3), kind)
        except Exception as e:  # noqa: BLE001
            row = (now(), tag, f"lost ({type(e).__name__})", None, kind)
        tester.waiting.pop(tag, None)
        pings.append(row)
        pw.writerow([row[0], row[1], row[4], row[2], row[3]])
        pf.flush()
        if row[2] != "ok":
            print(row[0], tag, kind, row[2], flush=True)

    async def sampler():
        while not stop.is_set():
            if gw.poll() is not None:
                print(now(), f"gateway exited with code {gw.returncode}", flush=True)
                stop.set()
                return
            rss, nfd = proc_stats(gw.pid)
            db = data / "gateway.db"
            media = data / "media"
            media_mb = size_mb(*[str(p) for p in media.glob("*")]) if media.exists() else 0.0
            s = (now(), round(rss, 1), nfd, round(size_mb(str(db), f"{db}-wal"), 3), round(media_mb, 2),
                 app.connects, tester.sessions)
            samples.append(s)
            sw.writerow(s)
            sf.flush()
            write_report(out, started, pings, samples, log_path, app, tester)
            ok = sum(p[2] == "ok" for p in pings)
            print(s[0], f"rss={s[1]}MB files={s[2]} db={s[3]}MB pings ok {ok}/{len(pings)}", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), args.sample)
            except asyncio.TimeoutError:
                pass

    st = asyncio.create_task(sampler())
    n = 0
    while not stop.is_set() and time.time() < end:
        n += 1
        asyncio.create_task(one_ping(n))
        try:
            await asyncio.wait_for(stop.wait(), args.interval)
        except asyncio.TimeoutError:
            pass
    stop.set()
    await asyncio.sleep(min(args.timeout, 5))
    await st
    write_report(out, started, pings, samples, log_path, app, tester)
    gw.send_signal(signal.SIGINT)
    try:
        gw.wait(15)
    except subprocess.TimeoutExpired:
        gw.kill()
    tester.stopping = True
    tester.disconnect()
    print(now(), f"done; see {out}/report.md", flush=True)
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
