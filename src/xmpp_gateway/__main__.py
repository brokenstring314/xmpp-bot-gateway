"""入口：加载配置，启动 XMPP 客户端与各协议出口。"""
import argparse
import asyncio
import logging

from .config import load_config
from .core.bus import EventBus
from .core.idmap import IdMap
from .xmpp.client import GatewayXMPP


async def run(config_path: str) -> None:
    cfg = load_config(config_path)
    idmap = IdMap(cfg.storage.db)
    await idmap.open()
    bus = EventBus()

    xmpp = GatewayXMPP(cfg.xmpp, bus, idmap)
    tasks = []

    if cfg.onebot_v11.enabled:
        from .protocols.onebot_v11.server import OneBotV11
        tasks.append(OneBotV11(cfg.onebot_v11, bus, idmap, xmpp).run())

    xmpp.connect()
    await asyncio.gather(*tasks)


def main() -> None:
    p = argparse.ArgumentParser(prog="xmpp-gateway")
    p.add_argument("-c", "--config", default="config.toml")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
