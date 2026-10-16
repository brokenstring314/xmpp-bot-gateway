"""XMPP 客户端（slixmpp）。P0：明文单聊 + MUC。P1 在此接入 OMEMO。"""
import logging
import time

import slixmpp

from ..core.events import MessageEvent, Scene, Segment

log = logging.getLogger(__name__)


class GatewayXMPP(slixmpp.ClientXMPP):
    def __init__(self, cfg, bus, idmap) -> None:
        super().__init__(cfg.jid, cfg.password)
        self.cfg, self.bus, self.idmap = cfg, bus, idmap
        for xep in ("xep_0030", "xep_0045", "xep_0199", "xep_0280", "xep_0313", "xep_0359", "xep_0363"):
            self.register_plugin(xep)
        self.add_event_handler("session_start", self._on_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("groupchat_message", self._on_groupchat)

    async def _on_start(self, _):
        self.send_presence()
        await self.get_roster()
        for room in self.cfg.rooms:
            self.plugin["xep_0045"].join_muc(room, self.cfg.nick)
        log.info("XMPP session started as %s", self.boundjid.bare)

    def _on_message(self, msg):
        if msg["type"] not in ("chat", "normal") or not msg["body"]:
            return
        self.bus.publish(MessageEvent(
            scene=Scene.PRIVATE, self_jid=self.boundjid.bare, sender_jid=msg["from"].bare,
            room_jid=None, message_id=msg["id"] or "", segments=[Segment("text", {"text": msg["body"]})],
            time=time.time(),
        ))

    def _on_groupchat(self, msg):
        if msg["mucnick"] == self.cfg.nick or not msg["body"]:
            return  # 忽略自己的回显
        self.bus.publish(MessageEvent(
            scene=Scene.GROUP, self_jid=self.boundjid.bare, sender_jid=str(msg["from"]),
            room_jid=msg["from"].bare, message_id=msg["id"] or "",
            segments=[Segment("text", {"text": msg["body"]})], time=time.time(),
        ))

    def send_text(self, to: str, text: str, group: bool) -> None:
        self.send_message(mto=to, mbody=text, mtype="groupchat" if group else "chat")
