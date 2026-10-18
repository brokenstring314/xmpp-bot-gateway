"""XMPP 客户端（slixmpp）：单聊 + MUC；可选 OMEMO 单聊加密。"""
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import slixmpp
from slixmpp.xmlstream import ET

from ..core.events import MessageEvent, Scene, Segment

log = logging.getLogger(__name__)

NS_DELAY = "{urn:xmpp:delay}delay"
NS_RETRACT = "urn:xmpp:message-retract:1"
NS_FALLBACK = "urn:xmpp:fallback:0"
NS_OOB = "jabber:x:oob"


class GatewayXMPP(slixmpp.ClientXMPP):
    def __init__(self, cfg, bus, idmap, omemo_cfg=None) -> None:
        super().__init__(cfg.jid, cfg.password)
        self.cfg, self.bus, self.idmap = cfg, bus, idmap
        for xep in ("xep_0030", "xep_0004", "xep_0045", "xep_0066", "xep_0199", "xep_0280",
                    "xep_0249", "xep_0313", "xep_0359", "xep_0363"):
            self.register_plugin(xep)
        self.omemo = bool(omemo_cfg and omemo_cfg.enabled)
        self._omemo_peers: set[str] = set()   # 最近一条消息是 OMEMO 加密的单聊对象 → 回复也加密
        if self.omemo:
            from . import omemo as omemo_mod
            store = Path(omemo_cfg.store_dir)
            self._peers_file = store / f"{cfg.jid.split('/')[0]}.peers.json"
            try:
                self._omemo_peers = set(json.loads(self._peers_file.read_text()))
            except (FileNotFoundError, ValueError):
                pass
            self.use_message_ids = True
            self.register_plugin("xep_0380")
            self.register_plugin("xep_0384", {
                "store_path": str(store / f"{cfg.jid.split('/')[0]}.json"),
                "btbv": omemo_cfg.trust != "manual",
            }, module=omemo_mod)
        self.online = False
        self._stopping = False
        self._backoff = 2
        self.add_event_handler("session_start", self._on_start)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("connection_failed", self._on_disconnected)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("groupchat_message", self._on_groupchat)
        self.add_event_handler("groupchat_invite", self._on_invite)          # XEP-0045 mediated
        self.add_event_handler("groupchat_direct_invite", self._on_invite)   # XEP-0249 direct

    # ---------- 连接 / 重连 ----------
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

    async def stop(self) -> None:
        self._stopping = True
        self.disconnect()

    async def _on_disconnected(self, reason=None) -> None:
        was_online, self.online = self.online, False
        if self._stopping:
            return
        if was_online:
            self._backoff = 2
        log.warning("XMPP disconnected (%s); reconnecting in %ss", reason, self._backoff)
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, 60)
        if not self._stopping and not self.online:
            self.start()

    async def _on_start(self, _):
        self.send_presence()
        await self.get_roster()
        self.online = True
        self._backoff = 2
        log.info("XMPP session started as %s", self.boundjid.bare)
        if self.omemo:
            try:  # 初始化会话管理器：生成/加载身份并发布设备列表与 bundle
                await self.plugin["xep_0384"].get_session_manager()
                log.info("OMEMO ready")
            except Exception as e:  # noqa: BLE001  OMEMO 失败不影响明文收发
                log.error("OMEMO init failed: %r", e)
        for room in self.cfg.rooms:
            try:
                await self.join_room(room)
            except Exception as e:  # noqa: BLE001  单个房间失败不影响其他房间与单聊
                log.error("failed to join room %s: %r", room, e)

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

    # ---------- 入站 ----------
    @staticmethod
    def _msg_id(msg) -> str:
        for sid in msg.xml.findall("{urn:xmpp:sid:0}stanza-id"):
            if sid.get("by") == msg["from"].bare and sid.get("id"):
                return sid.get("id")
        oid = msg.xml.find("{urn:xmpp:sid:0}origin-id")
        if oid is not None and oid.get("id"):
            return oid.get("id")
        return msg["id"] or uuid.uuid4().hex

    async def _on_invite(self, inv) -> None:
        if not self.cfg.accept_invites:
            return
        room = inv["groupchat_invite"]["jid"] if inv.xml.find("{jabber:x:conference}x") is not None else inv["from"].bare
        room = str(room).split("/")[0]
        if not room or room in self.rooms():
            return
        log.info("invited to %s by %s, joining", room, inv["from"])
        try:
            await self.join_room(room)
        except Exception as e:  # noqa: BLE001
            log.error("failed to join invited room %s: %s", room, e)

    def _set_omemo_peer(self, jid: str, encrypted: bool) -> None:
        if encrypted == (jid in self._omemo_peers):
            return
        (self._omemo_peers.add if encrypted else self._omemo_peers.discard)(jid)
        self._peers_file.parent.mkdir(parents=True, exist_ok=True)
        self._peers_file.write_text(json.dumps(sorted(self._omemo_peers)))

    async def _on_message(self, msg):
        # 解密需要网络往返（下载 bundle 等），用锁保证单聊消息按到达顺序推送
        if not hasattr(self, "_in_lock"):
            self._in_lock = asyncio.Lock()
        async with self._in_lock:
            await self._process_message(msg)

    async def _process_message(self, msg):
        if msg["type"] == "error":
            log.warning("error from %s: %s %s", msg["from"], msg["error"]["condition"], msg["error"]["text"])
            return
        if msg["type"] not in ("chat", "normal"):
            return
        encrypted = False
        if self.omemo and msg["from"].user and self.plugin["xep_0384"].is_encrypted(msg):
            if msg["from"].bare in self.rooms():
                log.info("ignoring OMEMO message via MUC %s (not supported)", msg["from"])
                return
            try:
                dec, dev = await self.plugin["xep_0384"].decrypt_message(msg)
            except Exception as e:  # noqa: BLE001
                log.warning("OMEMO decrypt failed for message from %s: %r", msg["from"], e)
                return
            log.debug("OMEMO message from %s device %s", dev.bare_jid, dev.device_id)
            if not dec["body"]:
                return  # 加密的回执/标记等，没有正文
            msg["body"] = dec["body"]
            encrypted = True
        if not msg["body"]:
            return
        if msg.xml.find("{jabber:x:conference}x") is not None or \
                msg.xml.find("{http://jabber.org/protocol/muc#user}x/{http://jabber.org/protocol/muc#user}invite") is not None:
            return  # 群邀请由 _on_invite 处理
        if not msg["from"].user:
            return  # 服务器本身发来的通知（如欢迎消息），不是用户消息
        if msg.xml.find(NS_DELAY) is not None and msg["type"] == "normal":
            return
        is_muc_pm = msg["from"].bare in self.rooms() and msg["from"].resource
        if self.omemo and not is_muc_pm:
            self._set_omemo_peer(msg["from"].bare, encrypted)
        # MUC 私信保留完整 room/nick 作为用户身份；普通单聊用 bare JID
        sender = str(msg["from"]) if is_muc_pm else msg["from"].bare
        roster_name = self.client_roster[msg["from"].bare]["name"] if not is_muc_pm else ""
        self.bus.publish(MessageEvent(
            scene=Scene.PRIVATE, self_jid=self.boundjid.bare, sender_jid=sender,
            room_jid=None, message_id=self._msg_id(msg),
            segments=[Segment("text", {"text": msg["body"]})], time=time.time(),
            sender_name=roster_name or (msg["from"].resource if is_muc_pm else msg["from"].user),
        ))

    def _on_groupchat(self, msg):
        if self.omemo and msg["mucnick"] != self.cfg.nick and self.plugin["xep_0384"].is_encrypted(msg):
            log.info("ignoring OMEMO group message in %s (group OMEMO not supported yet)", msg["from"].bare)
            return
        if msg["mucnick"] == self.cfg.nick or not msg["body"] or not msg["mucnick"]:
            return  # 忽略自己的回显和房间本身的通知
        if msg.xml.find(NS_DELAY) is not None:
            return  # 历史回放 / 离线补发的群消息，P0 先不推送
        room, nick = msg["from"].bare, msg["mucnick"]
        real = self.plugin["xep_0045"].get_jid_property(room, nick, "jid")
        sender = real.bare if real else str(msg["from"])  # 非匿名房间用真实 JID，跨房间 ID 一致
        body = msg["body"]
        segments: list[Segment] = []
        me = self.cfg.nick
        for sep in (":", ",", "，", "："):
            if body.startswith(me + sep):          # XMPP 惯例：「nick: 内容」即提及
                segments.append(Segment("at", {"target": self.boundjid.bare}))
                body = body[len(me) + 1:].lstrip()
                break
        segments.append(Segment("text", {"text": body}))
        self.bus.publish(MessageEvent(
            scene=Scene.GROUP, self_jid=self.boundjid.bare, sender_jid=sender,
            room_jid=room, message_id=self._msg_id(msg), segments=segments, time=time.time(),
            sender_name=nick,
            sender_role=self.plugin["xep_0045"].get_jid_property(room, nick, "affiliation") or "none",
        ))

    # ---------- 出站 ----------
    def rooms(self) -> list[str]:
        return [str(r) for r in self.plugin["xep_0045"].get_joined_rooms()]

    def is_encrypted_peer(self, to: str, group: bool) -> bool:
        return self.omemo and not group and to in self._omemo_peers

    async def send_rich(self, to: str, text: str, group: bool, urls: list[str] | None = None) -> str:
        """发送文本（可附带 OOB 链接），返回出站消息 id（同时作为 origin-id）。
        对方最近用 OMEMO 发来消息时加密回复；加密失败直接报错，不降级为明文。"""
        msg = self.make_message(mto=to, mbody=text, mtype="groupchat" if group else "chat")
        mid = uuid.uuid4().hex
        msg["id"] = mid
        if self.is_encrypted_peer(to, group):
            from slixmpp.jid import JID
            xep = self.plugin["xep_0384"]
            try:
                enc, errors = await xep.encrypt_message(msg, JID(to))
            except Exception as e:  # noqa: BLE001  设备列表可能过期：强制刷新后重试一次
                log.info("OMEMO encrypt to %s failed (%r); refreshing device list and retrying", to, e)
                await xep.refresh_device_lists({JID(to)}, force_download=True)
                enc, errors = await xep.encrypt_message(msg, JID(to))
            if errors:
                log.info("OMEMO non-critical encryption errors for %s: %s", to, errors)
            if enc is None:
                raise RuntimeError("OMEMO encryption produced no message")
            import oldmemo
            enc["eme"]["namespace"] = oldmemo.oldmemo.NAMESPACE
            enc["eme"]["name"] = "OMEMO"
            enc["id"] = mid
            ET.SubElement(enc.xml, "{urn:xmpp:sid:0}origin-id", {"id": mid})
            enc.send()   # URL 已在正文里；OOB 明文会泄露链接，加密时不附带
            return mid
        ET.SubElement(msg.xml, "{urn:xmpp:sid:0}origin-id", {"id": mid})  # XEP-0359
        if urls:
            msg["oob"]["url"] = urls[0]   # XEP-0066：客户端可内联显示
        msg.send()
        return mid

    def send_retract(self, to: str, origin_id: str, group: bool) -> None:
        """XEP-0424 撤回自己发出的消息。"""
        msg = self.make_message(mto=to, mtype="groupchat" if group else "chat",
                                mbody="This person attempted to retract a previous message, "
                                      "but it's unsupported by your client.")
        ET.SubElement(msg.xml, f"{{{NS_RETRACT}}}retract", {"id": origin_id})
        ET.SubElement(msg.xml, f"{{{NS_FALLBACK}}}fallback", {"for": NS_RETRACT})
        ET.SubElement(msg.xml, "{urn:xmpp:hints}store")
        msg.send()

    def nick_for(self, room: str, user: str) -> str | None:
        """把用户身份（真实 JID 或 room/nick）换成房间内昵称。"""
        if user.startswith(room + "/"):
            return user.split("/", 1)[1]
        muc = self.plugin["xep_0045"]
        for nick in muc.get_roster(room) or []:
            j = muc.get_jid_property(room, nick, "jid")
            if j and j.bare == user:
                return nick
        return None
