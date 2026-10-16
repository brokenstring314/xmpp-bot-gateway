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
        for xep in ("xep_0030", "xep_0004", "xep_0045", "xep_0199", "xep_0280", "xep_0313", "xep_0359", "xep_0363"):
            self.register_plugin(xep)
        self.add_event_handler("session_start", self._on_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("groupchat_message", self._on_groupchat)

    def start(self) -> None:
        """按配置连接：server 为空时走 DNS SRV；tls=none 仅用于本地测试服务器。"""
        tls = self.cfg.tls
        self.enable_direct_tls = tls == "direct"
        self.enable_starttls = tls == "starttls"
        self.enable_plaintext = tls == "none"
        if tls == "none":
            # 仅本地测试：允许在未加密连接上使用 PLAIN
            self.plugin["feature_mechanisms"].unencrypted_plain = True
        self.connect(host=self.cfg.server or None, port=self.cfg.port if self.cfg.server else None)

    async def _on_start(self, _):
        self.send_presence()
        await self.get_roster()
        log.info("XMPP session started as %s", self.boundjid.bare)
        for room in self.cfg.rooms:
            await self.join_room(room)

    async def join_room(self, room: str) -> None:
        muc = self.plugin["xep_0045"]
        # maxstanzas=0：不回放历史消息，避免把旧消息当新事件推给 bot
        res = await muc.join_muc_wait(room, self.cfg.nick, maxstanzas=0, timeout=30)
        pres = res[0] if isinstance(res, tuple) else res  # slixmpp>=1.8 返回 (presence, history)
        if 201 in pres["muc"]["status_codes"]:
            # 房间是本次新建的，处于锁定状态：提交默认配置，变为 instant room
            form = self.plugin["xep_0004"].make_form(ftype="submit")
            await muc.set_room_config(room, form)
            log.info("created instant room %s", room)
        log.info("joined room %s", room)

    def _on_message(self, msg):
        if msg["type"] not in ("chat", "normal") or not msg["body"]:
            return
        if not msg["from"].user:
            return  # 服务器本身发来的通知（如欢迎消息），不是用户消息
        if not msg["from"].user:
            return  # 服务器本身发来的通知（如欢迎消息），不是用户消息
        self.bus.publish(MessageEvent(
            scene=Scene.PRIVATE, self_jid=self.boundjid.bare, sender_jid=msg["from"].bare,
            room_jid=None, message_id=msg["id"] or "", segments=[Segment("text", {"text": msg["body"]})],
            time=time.time(),
        ))

    def _on_groupchat(self, msg):
        if msg["mucnick"] == self.cfg.nick or not msg["body"]:
            return  # 忽略自己的回显
        if msg.xml.find("{urn:xmpp:delay}delay") is not None:
            return  # 历史回放 / 离线补发的群消息，P0 先不推送
        if msg.xml.find("{urn:xmpp:delay}delay") is not None:
            return  # 历史回放 / 离线补发的群消息，P0 先不推送
        self.bus.publish(MessageEvent(
            scene=Scene.GROUP, self_jid=self.boundjid.bare, sender_jid=str(msg["from"]),
            room_jid=msg["from"].bare, message_id=msg["id"] or "",
            segments=[Segment("text", {"text": msg["body"]})], time=time.time(),
        ))

    def send_text(self, to: str, text: str, group: bool) -> None:
        self.send_message(mto=to, mbody=text, mtype="groupchat" if group else "chat")
