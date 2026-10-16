"""OneBot v11 出口（P0：反向 WS 客户端，连接 AstrBot / NoneBot）。

TODO(P0):
- 事件翻译：MessageEvent -> message.private / message.group（数字 ID 走 IdMap）
- 动作：send_private_msg / send_group_msg / send_msg / get_login_info
- 心跳 meta_event、生命周期 connect 事件、断线重连
"""
import asyncio
import json
import logging
import time

import aiohttp

from ...core.events import Scene

log = logging.getLogger(__name__)


class OneBotV11:
    def __init__(self, cfg, bus, idmap, xmpp) -> None:
        self.cfg, self.bus, self.idmap, self.xmpp = cfg, bus, idmap, xmpp

    async def run(self) -> None:
        await asyncio.gather(*(self._reverse_ws(url) for url in self.cfg.reverse_ws))

    async def _reverse_ws(self, url: str) -> None:
        self_id = await self.idmap.to_int("user", self.xmpp.boundjid.bare)
        headers = {"X-Self-ID": str(self_id), "X-Client-Role": "Universal"}
        if self.cfg.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.access_token}"
        events = self.bus.subscribe()
        while True:
            try:
                async with aiohttp.ClientSession() as s, s.ws_connect(url, headers=headers) as ws:
                    log.info("OneBot v11 reverse WS connected: %s", url)
                    await asyncio.gather(self._pump(ws, events, self_id), self._recv(ws))
            except Exception as e:  # noqa: BLE001
                log.warning("reverse WS %s dropped: %s; retry in 5s", url, e)
                await asyncio.sleep(5)

    async def _pump(self, ws, events, self_id: int) -> None:
        while True:
            ev = await events.get()
            await ws.send_str(json.dumps(await self._translate(ev, self_id), ensure_ascii=False))

    async def _translate(self, ev, self_id: int) -> dict:
        text = "".join(s.data.get("text", "") for s in ev.segments if s.type == "text")
        base = {
            "time": int(ev.time or time.time()), "self_id": self_id, "post_type": "message",
            "message_id": await self.idmap.to_int("message", ev.message_id or f"{ev.time}"),
            "message": [{"type": "text", "data": {"text": text}}], "raw_message": text, "font": 0,
            "user_id": await self.idmap.to_int("user", ev.sender_jid),
            "sender": {"user_id": await self.idmap.to_int("user", ev.sender_jid),
                       "nickname": ev.sender_jid.split("/")[-1]},
        }
        if ev.scene == Scene.GROUP:
            base |= {"message_type": "group", "sub_type": "normal",
                     "group_id": await self.idmap.to_int("group", ev.room_jid)}
        else:
            base |= {"message_type": "private", "sub_type": "friend"}
        return base

    async def _recv(self, ws) -> None:
        async for m in ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            req = json.loads(m.data)
            resp = await self._handle(req.get("action", ""), req.get("params", {}))
            resp["echo"] = req.get("echo")
            await ws.send_str(json.dumps(resp, ensure_ascii=False))

    async def _handle(self, action: str, p: dict) -> dict:
        def text_of(msg):
            if isinstance(msg, str):
                return msg
            return "".join(s.get("data", {}).get("text", "") for s in msg if s.get("type") == "text")

        if action in ("send_private_msg", "send_group_msg", "send_msg"):
            group = action == "send_group_msg" or p.get("message_type") == "group"
            target = await self.idmap.to_str("group" if group else "user",
                                             int(p.get("group_id") if group else p.get("user_id")))
            if not target:
                return {"status": "failed", "retcode": 100, "data": None}
            self.xmpp.send_text(target.split("/")[0] if group else target, text_of(p.get("message", "")), group)
            return {"status": "ok", "retcode": 0, "data": {"message_id": 0}}
        if action == "get_login_info":
            uid = await self.idmap.to_int("user", self.xmpp.boundjid.bare)
            return {"status": "ok", "retcode": 0, "data": {"user_id": uid, "nickname": self.xmpp.cfg.nick}}
        return {"status": "failed", "retcode": 1404, "data": None}
