"""Smoke test of the packaged gateway as a separate process: the Docker image, or `python -m xmpp_gateway`.

Usage: start tests/prosody/run.sh, then
    IMAGE=xmpp-bot-gateway:test python3 -u tests/docker_smoke.py   # the image (needs docker, host network)
    python3 -u tests/docker_smoke.py                               # no IMAGE: plain subprocess

Covers: `--check` on the shipped example config, start with OMEMO enabled (the optional
dependencies are installed), a private message round trip, state written to the data volume,
a clean and quick stop on SIGTERM (docker stop), and a restart on the same data.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import tomli_w
from aiohttp import web

sys.path.insert(0, os.path.dirname(__file__))
from harness import MockApp, User, check, results, settle, until  # noqa: E402

IMAGE = os.environ.get("IMAGE", "")
PORT = 6499
NAME = "xgw-smoke"
ACC = json.load(open("tests/prosody/run/accounts.json"))
BOT, USER = ACC["bot"]["jid"], ACC["user"]["jid"]


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class Gateway:
    """The gateway under test, as a container or a subprocess."""
    def __init__(self, cfg_path, data):
        self.cfg_path, self.data, self.proc = cfg_path, data, None

    def check_example(self):
        if IMAGE:
            r = sh("docker", "run", "--rm", "-v", f"{os.path.abspath('config.example.toml')}:/app/config.toml:ro",
                   IMAGE, "--check")
        else:
            r = sh(sys.executable, "-m", "xmpp_gateway", "-c", "config.example.toml", "--check",
                   env={**os.environ, "PYTHONPATH": "src"})
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def start(self, first=True):
        if IMAGE:
            if first:
                sh("docker", "rm", "-f", NAME)
                r = sh("docker", "run", "-d", "--name", NAME, "--network", "host",
                       "-v", f"{self.cfg_path}:/app/config.toml:ro", "-v", f"{self.data}:/app/data", IMAGE)
            else:
                r = sh("docker", "start", NAME)
            assert r.returncode == 0, r.stderr
        else:
            self.log = open(os.path.join(self.data, "stdout.log"), "a")
            self.proc = subprocess.Popen([sys.executable, "-m", "xmpp_gateway", "-c", self.cfg_path],
                                         cwd=self.data, stdout=self.log, stderr=subprocess.STDOUT,
                                         env={**os.environ, "PYTHONPATH": os.path.abspath("src")})

    def stop(self):
        """SIGTERM; returns (exit code, seconds taken)."""
        t0 = time.time()
        if IMAGE:
            sh("docker", "stop", "-t", "15", NAME)
            code = int(sh("docker", "inspect", "-f", "{{.State.ExitCode}}", NAME).stdout.strip() or -1)
        else:
            self.proc.terminate()
            try:
                code = self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                code = self.proc.wait()
        return code, round(time.time() - t0, 1)

    def logs(self):
        if IMAGE:
            r = sh("docker", "logs", NAME)
            return r.stdout + r.stderr
        return open(os.path.join(self.data, "stdout.log")).read()

    def cleanup(self):
        if IMAGE:
            sh("docker", "rm", "-f", NAME)
        elif self.proc and self.proc.poll() is None:
            self.proc.kill()


async def main():
    work = tempfile.mkdtemp(prefix="xgw-smoke-")
    data = os.path.join(work, "data")
    os.makedirs(data)
    os.chmod(data, 0o777)   # the image runs as uid 10001
    # paths are relative: inside the image they resolve under /app, i.e. in the data volume
    rel = "data/" if IMAGE else ""
    cfg = {"xmpp": {**ACC["bot"], "server": "127.0.0.1", "port": 15222, "tls": "none", "rooms": [],
                    "rate_limit": 0, "mam_catchup": False},
           "omemo": {"enabled": True, "store_dir": f"{rel}omemo"},
           "media": {"dir": f"{rel}media"},
           "storage": {"db": f"{rel}gateway.db"},
           "onebot_v11": {"reverse_ws": [f"ws://127.0.0.1:{PORT}/ws"], "reconnect_max": 1}}
    cfg_path = os.path.join(work, "config.toml")
    open(cfg_path, "wb").write(tomli_w.dumps(cfg).encode())
    os.chmod(cfg_path, 0o644)

    app = MockApp()
    wa = web.Application()
    wa.router.add_get("/ws", app.handler)
    runner = web.AppRunner(wa, shutdown_timeout=0.5)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    u = User(USER, ACC["user"]["password"])
    u.enable_starttls = u.enable_direct_tls = False
    u.enable_plaintext = True
    u["feature_mechanisms"].unencrypted_plain = True
    u.connect(host="127.0.0.1", port=15222)
    await asyncio.wait_for(u.ready.wait(), 15)

    gw = Gateway(cfg_path, data)
    try:
        ok, out = gw.check_example()
        check("--check accepts the shipped config.example.toml", ok, out)

        gw.start()
        up = await until(lambda: app.ws is not None, 60)
        check("gateway starts with OMEMO enabled and connects to the framework", up, gw.logs()[-2000:])
        await settle(app)

        async def round_trip(tag):
            k = len(app.events)
            u.send_message(mto=BOT, mbody=f"ping {tag}", mtype="chat")
            ev = await until(lambda: next((e for e in app.events[k:] if e["raw_message"] == f"ping {tag}"), None), 20)
            n = len(u.inbox)
            if ev:
                await app.call("send_private_msg", user_id=ev["user_id"], message=f"pong {tag}")
            got = await until(lambda: u.got(lambda m: m["body"] == f"pong {tag}", n), 15)
            return bool(ev), bool(got)

        ev, got = await round_trip("1")
        check("private message reaches the framework", ev)
        check("framework reply reaches the user", got)
        check("state is written to the data directory",
              os.path.exists(os.path.join(data, "gateway.db")) and os.path.isdir(os.path.join(data, "omemo")),
              os.listdir(data))

        app.ws = None
        code, secs = gw.stop()
        check("SIGTERM stops the gateway cleanly and quickly", code == 0 and secs < 10, (code, secs))
        check("shutdown is logged", "shutting down" in gw.logs(), gw.logs()[-1000:])

        gw.start(first=False)
        up = await until(lambda: app.ws is not None, 60)
        check("restart on the same data comes back", up, gw.logs()[-2000:])
        ev, got = await round_trip("2")
        check("round trip works after the restart", ev and got, (ev, got))
        gw.stop()
    finally:
        gw.cleanup()
        u.disconnect()
        await runner.cleanup()
        shutil.rmtree(work, ignore_errors=True)

    print(f"RESULT {sum(results.values())}/{len(results)} passed", flush=True)
    os._exit(0 if all(results.values()) else 1)


asyncio.run(main())
