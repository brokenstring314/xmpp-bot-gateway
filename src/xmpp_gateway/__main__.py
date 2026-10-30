"""入口：加载配置，启动 XMPP 客户端与各协议出口。"""
import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any

from .config import load_config
from .core.bus import EventBus
from .core.idmap import IdMap
from .xmpp.client import GatewayXMPP

log = logging.getLogger("xmpp_gateway")


def _try_lock(fd: int) -> None:
    """非阻塞独占锁；已被占用时抛 OSError。Linux/macOS 用 flock，Windows 用 msvcrt。"""
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class Gateway:
    def __init__(self, config_path: str) -> None:
        self.cfg = load_config(config_path)
        self.idmap = IdMap(self.cfg.storage.db, self.cfg.storage.keep_days)
        self.bus = EventBus()
        self.xmpp = GatewayXMPP(self.cfg.xmpp, self.bus, self.idmap, self.cfg.omemo, self.cfg.media)
        self.onebot: Any = None
        self._lock_fd: int | None = None
        self._tasks: list[asyncio.Task] = []

    def _lock(self) -> None:
        """同一数据库只允许一个实例：两个实例共用账号和 OMEMO 存储会互相覆盖设备与会话。"""
        path = self.cfg.storage.db + ".lock"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock_fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _try_lock(self._lock_fd)
        except OSError:
            os.close(self._lock_fd)
            raise SystemExit(f"another gateway instance is already using {self.cfg.storage.db}") from None

    async def start(self) -> None:
        self._lock()
        await self.idmap.open()
        if self.cfg.onebot_v11.enabled:
            from .protocols.onebot_v11.server import OneBotV11
            self.onebot = OneBotV11(self.cfg.onebot_v11, self.bus, self.idmap, self.xmpp)
            self._tasks.append(asyncio.create_task(self.onebot.run()))
        if self.cfg.satori.enabled:
            log.warning("[satori] enabled = true, but Satori is not implemented yet; ignoring it")
        self.xmpp.start()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.xmpp.stop()
        await self.idmap.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None


async def run(config_path: str) -> None:
    gw = Gateway(config_path)
    stop = asyncio.Event()
    if sys.platform != "win32":
        # docker stop / systemctl stop send SIGTERM; as PID 1 in a container the default action would be ignored
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    await gw.start()
    try:
        await stop.wait()
        log.info("shutting down")
    finally:
        await gw.stop()


def setup_logging(lc) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if lc.file:
        from logging.handlers import RotatingFileHandler
        os.makedirs(os.path.dirname(lc.file) or ".", exist_ok=True)
        handlers.append(RotatingFileHandler(lc.file, maxBytes=lc.max_mb * 1024 * 1024,
                                            backupCount=lc.backups, encoding="utf-8"))
    root = logging.getLogger()
    root.setLevel(lc.level)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


def main() -> None:
    p = argparse.ArgumentParser(prog="xmpp-gateway")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("--check", action="store_true", help="validate the config file and exit")
    args = p.parse_args()
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:   # pydantic ValidationError 是 ValueError 的子类
        raise SystemExit(f"config error in {args.config}:\n{e}") from None
    if args.check:
        print(f"{args.config}: OK")
        return
    setup_logging(cfg.log)
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
