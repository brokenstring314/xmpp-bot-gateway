"""XMPP 客户端（slixmpp）：单聊 + MUC；可选 OMEMO 单聊加密。"""
import asyncio
import hashlib
import io
import json
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import slixmpp
from slixmpp.xmlstream import ET

from ..config import MediaCfg
from ..core.events import MessageEvent, RecallEvent, Scene, Segment
from . import media

log = logging.getLogger(__name__)

NS_DELAY = "{urn:xmpp:delay}delay"
NS_REPLY = "urn:xmpp:reply:0"
NS_REF = "urn:xmpp:reference:0"
NS_FALLBACK = "urn:xmpp:fallback:0"
NS_RETRACT = "urn:xmpp:message-retract:1"
NS_OOB = "jabber:x:oob"


class GatewayXMPP(slixmpp.ClientXMPP):
    def __init__(self, cfg, bus, idmap, omemo_cfg=None, media_cfg=None) -> None:
        super().__init__(cfg.jid, cfg.password)
        self.cfg, self.bus, self.idmap = cfg, bus, idmap
        self.media_cfg = media_cfg or MediaCfg()
        for xep in ("xep_0030", "xep_0004", "xep_0045", "xep_0066", "xep_0199", "xep_0280",
                    "xep_0249", "xep_0313", "xep_0359", "xep_0363"):
            self.register_plugin(xep)
        # XEP-0184：对方请求回执时自动回执（客户端显示「已送达」）
        self.register_plugin("xep_0184", {"auto_ack": True, "auto_request": False})
        self._seen: OrderedDict[str, None] = OrderedDict()   # 最近推送过的「会话|消息 id」，MAM 补拉去重
        self._catchup_task: asyncio.Task | None = None
        self._reflected: dict[tuple[str, str], str] = {}  # (room, 自己消息的 origin-id) -> 房间分配的 stanza-id
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
        if self.cfg.mam_catchup:
            self._catchup_task = asyncio.create_task(self._catch_up())

    # ---------- MAM 补拉（XEP-0313） ----------
    async def _catch_up(self) -> None:
        archives = [("private", None)] + [(r, r) for r in self.rooms()]
        for key, room in archives:
            try:
                await self._catch_up_one(key, room)
            except Exception as e:  # noqa: BLE001  服务器/房间不支持 MAM 等
                log.warning("MAM catch-up for %s failed: %r", room or "private chats", e)

    async def _catch_up_one(self, key: str, room: str | None) -> None:
        ck = await self.idmap.get_state(f"mam:{key}")
        now = time.time()
        if ck is None:   # 首次运行：不回放旧消息，只从现在开始记
            await self.idmap.set_state(f"mam:{key}", str(now))
            await self.idmap.set_state(f"mam:{key}:floor", str(now))
            return
        floor = float(await self.idmap.get_state(f"mam:{key}:floor") or 0)
        # 留 30s 重叠（靠去重消化），但不早于首次运行时间
        since = max(float(ck) - 30, floor, now - self.cfg.mam_max_age)
        start = datetime.fromtimestamp(since, tz=timezone.utc)
        mam = self.plugin["xep_0313"]
        n = 0
        async for res in mam.iterate(jid=slixmpp.JID(room) if room else None, start=start,
                                     total=self.cfg.mam_max_messages, rsm={"max": 25}):
            fwd = res["mam_result"]["forwarded"]
            inner = fwd["stanza"]
            stamp = fwd["delay"]["stamp"]
            ts = stamp.timestamp() if stamp else now
            if room:
                if inner["type"] != "groupchat":
                    continue
                n += await self._handle_group(inner, archived_ts=ts, archive_id=res["mam_result"]["id"])
            else:
                if inner["from"].bare == self.boundjid.bare or inner["type"] not in ("chat", "normal"):
                    continue   # 自己发出的（含其他设备）
                async with self._in_lock:
                    n += await self._process_message(inner, archived_ts=ts)
        if n:
            log.info("MAM catch-up: %d missed message(s) from %s", n, room or "private chats")

    def _fresh(self, chat: str, mid: str) -> bool:
        k = f"{chat}|{mid}"
        if k in self._seen:
            return False
        self._seen[k] = None
        if len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return True

    async def _emit(self, ev: MessageEvent, key: str) -> int:
        """去重后推送事件，并推进该档案的补拉检查点。返回是否推送（1/0）。"""
        chat = ev.room_jid or ev.sender_jid
        if isinstance(ev, RecallEvent):
            self.bus.publish(ev)
            return 1
        if not self._fresh(chat, ev.message_id):
            return 0
        if ev.archived and await self.idmap.find_message(chat, ev.message_id) is not None:
            return 0
        self.bus.publish(ev)
        ck = await self.idmap.get_state(f"mam:{key}")
        if ck is None or ev.time > float(ck):
            await self.idmap.set_state(f"mam:{key}", str(ev.time))
        return 1

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

    @staticmethod
    def _take_reply(msg, body: str) -> tuple[str, Segment | None]:
        """XEP-0461：取出被回复消息的 id，并按 XEP-0428 去掉正文里的引用回退文本。"""
        r = msg.xml.find(f"{{{NS_REPLY}}}reply")
        if r is None or not r.get("id"):
            return body, None
        quote = ""
        for fb in msg.xml.findall(f"{{{NS_FALLBACK}}}fallback"):
            if fb.get("for") != NS_REPLY:
                continue
            b = fb.find(f"{{{NS_FALLBACK}}}body")
            if b is None:
                break
            try:
                start, end = int(b.get("start", 0)), int(b.get("end", 0))
            except ValueError:
                break
            if 0 <= start < end <= len(body):
                quote, body = body[start:end], body[:start] + body[end:]
            break
        return body, Segment("reply", {"xmpp_id": r.get("id"), "fallback": quote})

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

    @property
    def _in_lock(self) -> asyncio.Lock:
        if not hasattr(self, "_in_lock_obj"):
            self._in_lock_obj = asyncio.Lock()
        return self._in_lock_obj

    async def _on_message(self, msg):
        # 解密需要网络往返（下载 bundle 等），用锁保证单聊消息按到达顺序推送
        async with self._in_lock:
            await self._process_message(msg)

    async def _process_message(self, msg, archived_ts: float | None = None) -> int:
        if msg["type"] == "error":
            log.warning("error from %s: %s %s", msg["from"], msg["error"]["condition"], msg["error"]["text"])
            return 0
        if msg["type"] not in ("chat", "normal"):
            return 0
        retract = msg.xml.find(f"{{{NS_RETRACT}}}retract")
        if retract is not None and retract.get("id") and msg["from"].user:
            is_pm = msg["from"].bare in self.rooms() and msg["from"].resource
            return await self._emit(RecallEvent(
                scene=Scene.PRIVATE, self_jid=self.boundjid.bare,
                sender_jid=str(msg["from"]) if is_pm else msg["from"].bare, room_jid=None,
                target_id=retract.get("id"), time=archived_ts or time.time(),
                archived=archived_ts is not None), "private")
        if archived_ts is not None and msg["from"].user and \
                await self.idmap.find_message(msg["from"].bare, self._msg_id(msg)) is not None:
            return 0   # 已推送过（在线收到或离线存储已投递），也避免重复解密 OMEMO
        encrypted = False
        if self.omemo and msg["from"].user and self.plugin["xep_0384"].is_encrypted(msg):
            if msg["from"].bare in self.rooms():
                log.info("ignoring OMEMO message via MUC %s (not supported)", msg["from"])
                return 0
            try:
                dec, dev = await self.plugin["xep_0384"].decrypt_message(msg)
            except Exception as e:  # noqa: BLE001
                log.warning("OMEMO decrypt failed for message from %s: %r", msg["from"], e)
                return 0
            log.debug("OMEMO message from %s device %s", dev.bare_jid, dev.device_id)
            if not dec["body"]:
                return 0  # 加密的回执/标记等，没有正文
            msg["body"] = dec["body"]
            encrypted = True
        if not msg["body"]:
            return 0
        if msg.xml.find("{jabber:x:conference}x") is not None or \
                msg.xml.find("{http://jabber.org/protocol/muc#user}x/{http://jabber.org/protocol/muc#user}invite") is not None:
            return 0  # 群邀请由 _on_invite 处理
        if not msg["from"].user:
            return 0  # 服务器本身发来的通知（如欢迎消息），不是用户消息
        if archived_ts is None and msg.xml.find(NS_DELAY) is not None and msg["type"] == "normal":
            return 0
        is_muc_pm = msg["from"].bare in self.rooms() and msg["from"].resource
        if self.omemo and not is_muc_pm:
            self._set_omemo_peer(msg["from"].bare, encrypted)
        # MUC 私信保留完整 room/nick 作为用户身份；普通单聊用 bare JID
        sender = str(msg["from"]) if is_muc_pm else msg["from"].bare
        roster_name = self.client_roster[msg["from"].bare]["name"] if not is_muc_pm else ""
        body, reply = self._take_reply(msg, msg["body"])
        msg["body"] = body
        segments = await self._media_segments(msg) or [Segment("text", {"text": body})]
        if reply:
            segments.insert(0, reply)
        return await self._emit(MessageEvent(
            scene=Scene.PRIVATE, self_jid=self.boundjid.bare, sender_jid=sender,
            room_jid=None, message_id=self._msg_id(msg), encrypted=encrypted,
            segments=segments, time=archived_ts or time.time(), archived=archived_ts is not None,
            sender_name=roster_name or (msg["from"].resource if is_muc_pm else msg["from"].user),
        ), "private")

    async def _media_segments(self, msg) -> list[Segment] | None:
        """正文仅为一个媒体链接时转成 image/record/video 段：
        - http(s) 链接且与 OOB 一致（Conversations 等客户端的 HTTP Upload 分享方式）
        - aesgcm://（OMEMO 会话中的加密文件，XEP-0454）：下载解密后存到本地，给出 file:// 路径"""
        url = msg["body"].strip()
        if not url or any(c.isspace() for c in url):
            return None
        if url.startswith("aesgcm://"):
            if media.kind_of(url) is None:
                return None
            try:
                path = await self._fetch_aesgcm(url)
            except Exception as e:  # noqa: BLE001
                log.warning("failed to fetch aesgcm media from %s: %r", msg["from"], e)
                return None
            uri = Path(path).resolve().as_uri()
            return [Segment(media.kind_of(url), {"file": uri, "url": uri})]
        oob = msg["oob"]["url"] if msg.xml.find("{jabber:x:oob}x") is not None else ""
        if url.startswith(("http://", "https://")) and oob == url and media.kind_of(url):
            return [Segment(media.kind_of(url), {"file": url, "url": url})]
        return None

    async def _fetch_aesgcm(self, url: str) -> str:
        import aiohttp
        src, frag = media.aesgcm_split(url, self.media_cfg.aesgcm_insecure_http)
        limit = self.media_cfg.max_size_mb * 1024 * 1024
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
            async with http.get(src) as r:
                r.raise_for_status()
                if int(r.headers.get("Content-Length") or 0) > limit:
                    raise ValueError("file too large")
                payload = await r.content.read(limit + 17)
                if len(payload) > limit + 16:
                    raise ValueError("file too large")
        data = media.aesgcm_decrypt(payload, frag)
        d = Path(self.media_cfg.dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{hashlib.sha256(data).hexdigest()[:24]}.{media.ext_of(src) or 'bin'}"
        path.write_bytes(data)
        return str(path)

    def _on_groupchat(self, msg):
        asyncio.ensure_future(self._handle_group(msg))

    async def _handle_group(self, msg, archived_ts: float | None = None, archive_id: str | None = None) -> int:
        if self.omemo and msg["mucnick"] != self.cfg.nick and self.plugin["xep_0384"].is_encrypted(msg):
            log.info("ignoring OMEMO group message in %s (group OMEMO not supported yet)", msg["from"].bare)
            return 0
        if msg["mucnick"] == self.cfg.nick:
            oid = msg.xml.find("{urn:xmpp:sid:0}origin-id")
            sid = self._msg_id(msg)
            if oid is not None and oid.get("id") and sid != oid.get("id"):
                if len(self._reflected) > 2000:
                    self._reflected.clear()
                self._reflected[(msg["from"].bare, oid.get("id"))] = sid
                self._reflected[(msg["from"].bare, "@" + sid)] = oid.get("id")
        retract = msg.xml.find(f"{{{NS_RETRACT}}}retract")
        if retract is not None and retract.get("id") and msg["mucnick"] and msg["mucnick"] != self.cfg.nick:
            room, nick = msg["from"].bare, msg["mucnick"]
            real = self.plugin["xep_0045"].get_jid_property(room, nick, "jid")
            return await self._emit(RecallEvent(
                scene=Scene.GROUP, self_jid=self.boundjid.bare,
                sender_jid=real.bare if real else str(msg["from"]), room_jid=room,
                target_id=retract.get("id"), time=archived_ts or time.time(),
                archived=archived_ts is not None), room)
        if msg["mucnick"] == self.cfg.nick or not msg["body"] or not msg["mucnick"]:
            return 0  # 忽略自己的回显和房间本身的通知
        if archived_ts is None and msg.xml.find(NS_DELAY) is not None:
            return 0  # 加入时的历史回放：由 MAM 补拉统一处理
        room, nick = msg["from"].bare, msg["mucnick"]
        real = self.plugin["xep_0045"].get_jid_property(room, nick, "jid")
        sender = real.bare if real else str(msg["from"])  # 非匿名房间用真实 JID，跨房间 ID 一致
        body, reply = self._take_reply(msg, msg["body"])
        segments: list[Segment] = [reply] if reply else []
        me = self.cfg.nick
        for sep in (":", ",", "，", "："):
            if body.startswith(me + sep):          # XMPP 惯例：「nick: 内容」即提及
                segments.append(Segment("at", {"target": self.boundjid.bare}))
                body = body[len(me) + 1:].lstrip()
                break
        else:
            mine = {f"xmpp:{self.boundjid.bare}", f"xmpp:{room}/{me}"}
            if any(r.get("type") == "mention" and r.get("uri") in mine
                   for r in msg.xml.findall(f"{{{NS_REF}}}reference")):   # XEP-0372
                segments.append(Segment("at", {"target": self.boundjid.bare}))
        oob = msg["oob"]["url"] if msg.xml.find("{jabber:x:oob}x") is not None else ""
        url = body.strip()
        if url and url == oob and url.startswith(("http://", "https://")) and media.kind_of(url):
            segments.append(Segment(media.kind_of(url), {"file": url, "url": url}))
        else:
            segments.append(Segment("text", {"text": body}))
        return await self._emit(MessageEvent(
            scene=Scene.GROUP, self_jid=self.boundjid.bare, sender_jid=sender,
            room_jid=room, message_id=archive_id or self._msg_id(msg), segments=segments,
            time=archived_ts or time.time(), archived=archived_ts is not None,
            sender_name=nick,
            sender_role=self.plugin["xep_0045"].get_jid_property(room, nick, "affiliation") or "none",
        ), f"{room}")

    # ---------- 出站 ----------
    def rooms(self) -> list[str]:
        return [str(r) for r in self.plugin["xep_0045"].get_joined_rooms()]

    def is_encrypted_peer(self, to: str, group: bool) -> bool:
        return self.omemo and not group and to in self._omemo_peers

    def room_id_of_own(self, room: str, origin_id: str) -> str:
        """自己发到群里的消息：返回房间分配的 stanza-id（回复引用需要它），未知时退回 origin-id。"""
        return self._reflected.get((room, origin_id), origin_id)

    def origin_of_reflection(self, room: str, stanza_id: str) -> str | None:
        return self._reflected.get((room, "@" + stanza_id))

    @staticmethod
    def _add_reply(stanza, reply: tuple[str, str, str] | None) -> None:
        if not reply:
            return
        to, rid, fb_len = reply[0], reply[1], int(reply[2])
        ET.SubElement(stanza.xml, f"{{{NS_REPLY}}}reply", {"to": to, "id": rid})
        if fb_len:
            fb = ET.SubElement(stanza.xml, f"{{{NS_FALLBACK}}}fallback", {"for": NS_REPLY})
            ET.SubElement(fb, f"{{{NS_FALLBACK}}}body", {"start": "0", "end": str(fb_len)})

    async def send_rich(self, to: str, text: str, group: bool, urls: list[str] | None = None,
                        reply: tuple[str, str, str] | None = None,
                        mentions: list[tuple[int, int, str]] | None = None) -> str:
        """reply = (被回复者 JID, 被回复消息 id, 正文开头回退引用的长度)。"""
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
            self._add_reply(enc, reply)
            enc.send()   # URL 已在正文里；OOB 明文会泄露链接，加密时不附带
            return mid
        ET.SubElement(msg.xml, "{urn:xmpp:sid:0}origin-id", {"id": mid})  # XEP-0359
        if urls:
            msg["oob"]["url"] = urls[0]   # XEP-0066：客户端可内联显示
        self._add_reply(msg, reply)
        for begin, end, uri in mentions or []:   # XEP-0372：偏移按 Unicode 码位
            ET.SubElement(msg.xml, f"{{{NS_REF}}}reference",
                          {"type": "mention", "begin": str(begin), "end": str(end), "uri": uri})
        msg.send()
        return mid

    async def upload(self, data: bytes, filename: str, content_type: str, encrypt: bool) -> str:
        """XEP-0363 上传；encrypt=True 时先按 XEP-0454 加密，返回 aesgcm:// 链接。"""
        up = self.plugin["xep_0363"]
        if encrypt:
            payload, frag = media.aesgcm_encrypt(data)
            url = await up.upload_file(filename, size=len(payload), content_type="application/octet-stream",
                                       input_file=io.BytesIO(payload), timeout=60)
            return media.aesgcm_url(url, frag)
        return await up.upload_file(filename, size=len(data), content_type=content_type,
                                    input_file=io.BytesIO(data), timeout=60)

    def send_retract(self, to: str, origin_id: str, group: bool) -> None:
        """XEP-0424 撤回自己发出的消息（origin_id 可为逗号分隔的多条）。"""
        if "," in origin_id:
            for oid in origin_id.split(","):
                self.send_retract(to, oid, group)
            return
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
