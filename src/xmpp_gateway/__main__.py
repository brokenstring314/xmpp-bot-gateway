"""入口：加载配置，启动 XMPP 客户端与各协议出口。"""
import argparse
import asyncio
import logging

from .config import load_config
from .core.bus import EventBus
from .core.idmap import IdMap
from .xmpp.client import GatewayXMPP


class Gateway:
    def __init__(self, config_path: str) -> None:
        self.cfg = load_config(config_path)
        self.idmap = IdMap(self.cfg.storage.db, self.cfg.storage.keep_days)
        self.bus = EventBus()
        self.xmpp = GatewayXMPP(self.cfg.xmpp, self.bus, self.idmap, self.cfg.omemo, self.cfg.media)
        self.onebot = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.idmap.open()
        if self.cfg.onebot_v11.enabled:
            from .protocols.onebot_v11.server import OneBotV11
            self.onebot = OneBotV11(self.cfg.onebot_v11, self.bus, self.idmap, self.xmpp)
            self._tasks.append(asyncio.create_task(self.onebot.run()))
        self.xmpp.start()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.xmpp.stop()
        await self.idmap.close()


async def run(config_path: str) -> None:
    gw = Gateway(config_path)
    await gw.start()
    try:
        await asyncio.Event().wait()
    finally:
        await gw.stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="xmpp-gateway")
    p.add_argument("-c", "--config", default="config.toml")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
