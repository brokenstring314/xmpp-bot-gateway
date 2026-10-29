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
from typing import Any, cast

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
NS_OCC = "urn:xmpp:occupant-id:0"
OCC_PREFIX = "occupant-id:"


def _log_failure(t: asyncio.Future) -> None:
    if not t.cancelled() and t.exception():
        log.error("message handler failed", exc_info=t.exception())


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
        self._rate: dict[str, list[float]] = {}
        self._reflected: dict[tuple[str, str], str] = {}  # (room, 自己消息的 origin-id) -> 房间分配的 stanza-id
        self.omemo = bool(omemo_cfg and omemo_cfg.enabled)
        self.omemo_cfg = omemo_cfg
        self._resynced: dict[tuple[str, int], float] = {}   # (JID, 设备 id) -> 上次重建会话的时间
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
        self.add_event_handler("groupchat_presence", self._on_muc_presence)
        self._occ_rooms: set[str] = set()                 # 声明支持 XEP-0421 的房间（只有这些房间的 occupant-id 可信）
        self._occ: dict[tuple[str, str], str] = {}       # (room, nick) -> occupant-id
        self._nick_rooms_warned: set[str] = set()
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
        """先停止处理入站消息和补拉，再等连接真正关闭：关闭过程中到达的消息不处理（也不标记为已推送），
        下次启动由 MAM 补拉，避免在数据库关闭后半处理而丢消息。"""
        self._stopping = True
        if self._catchup_task is not None and not self._catchup_task.done():
            self._catchup_task.cancel()
        if self.online or self.transport is not None:
            closed = asyncio.Event()
            self.add_event_handler("disconnected", lambda *_: closed.set(), disposable=True)
            self.disconnect()
            try:
                await asyncio.wait_for(closed.wait(), 5)
            except asyncio.TimeoutError:
                self.abort()
        self.online = False

    async def _on_disconnected(self, reason=None) -> None:
        was_online, self.online = self.online, False
        if self._stopping:
            return
        if was_online:
            self._backoff = 2
        log.warning("XMPP disconnected (%s); reconnecting in %ss", reason, self._backoff)
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, max(self.cfg.reconnect_max, 1))
        if not self._stopping and not self.online:
            self.start()

    async def _on_start(self, _):
        self.send_presence(pstatus=self.cfg.status or None)
        await self.get_roster()
        self.online = True
        self._backoff = 2
        log.info("XMPP session started as %s", self.boundjid.bare)
        if self.omemo:
            try:  # 初始化会话管理器：生成/加载身份并发布设备列表与 bundle
                await self._omemo_plugin.get_session_manager()
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
        start = datetime.fromtimestamp(int(since), tz=timezone.utc)   # 整秒：兼容只解析整秒的服务器
        mam = self.plugin["xep_0313"]
        n = 0
        async for res in mam.iterate(jid=slixmpp.JID(room) if room else None, start=start,
                                     total=self.cfg.mam_max_messages, rsm={"max": 25}):
            if res["from"] and res["from"].bare != (room or self.boundjid.bare):
                log.warning("ignoring MAM result from unexpected sender %s", res["from"])
                continue
            fwd = res["mam_result"]["forwarded"]
            inner = fwd["stanza"]
            stamp = fwd["delay"]["stamp"]
            ts = stamp.timestamp() if stamp else now
            if ts < since:
                continue   # 有的服务器（实测 Prosody 13）不认带小数秒的 start，会把更早的归档也返回
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

    def _allow(self, sender: str) -> bool:
        """每个发送者每分钟最多 rate_limit 条（滑动窗口）。"""
        limit = self.cfg.rate_limit
        if limit <= 0:
            return True
        now = time.monotonic()
        q = self._rate.setdefault(sender, [])
        q[:] = [t for t in q if now - t < 60]
        if len(q) >= limit:
            if len(q) == limit:
                log.warning("rate limit: dropping messages from %s", sender)
                q.append(now)   # 只记一次警告
            return False
        q.append(now)
        if len(self._rate) > 10000:
            self._rate.clear()
        return True

    def _fresh(self, chat: str, mid: str) -> bool:
        k = f"{chat}|{mid}"
        if k in self._seen:
            return False
        self._seen[k] = None
        if len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return True

    @property
    def _omemo_plugin(self) -> Any:
        """slixmpp-omemo 注册的 xep_0384 插件（不在 slixmpp 自带的插件类型表里）。"""
        return cast(Any, self.plugin)["xep_0384"]

    async def _emit(self, ev: MessageEvent | RecallEvent, key: str) -> int:
        """去重后推送事件，并推进该档案的补拉检查点。返回是否推送（1/0）。"""
        chat = ev.room_jid or ev.sender_jid
        if isinstance(ev, RecallEvent):
            self.bus.publish(ev)
            return 1
        if not self._fresh(chat, ev.message_id):
            return 0
        if not ev.archived and not self._allow(ev.sender_jid):
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
        try:
            res = await muc.join_muc_wait(slixmpp.JID(room), self.cfg.nick, maxstanzas=0,
                                          timeout=self.cfg.join_timeout)
        except BaseException:
            # slixmpp 在发出加入请求时就把房间记为已加入；失败（超时/报错）后要撤销，否则
            # rooms() 会把没进去的房间当成已加入：伪造的该房间群消息会被接受，发群消息也不会报错
            jid = slixmpp.JID(room)
            for table in (muc.rooms, muc.our_nicks):
                table.get(None, {}).pop(jid, None)
            raise
        pres = res[0] if isinstance(res, tuple) else res  # slixmpp>=1.8 返回 (presence, history)
        if 201 in pres["muc"]["status_codes"]:
            # 房间是本次新建的，处于锁定状态：提交默认配置，变为 instant room
            form = self.plugin["xep_0004"].make_form(ftype="submit")
            await muc.set_room_config(room, form)
            log.info("created instant room %s", room)
        try:
            info = await self.plugin["xep_0030"].get_info(jid=slixmpp.JID(room), timeout=10)
            if NS_OCC in info["disco_info"]["features"]:
                self._occ_rooms.add(room)
            else:
                self._occ_rooms.discard(room)
        except Exception as e:  # noqa: BLE001
            log.debug("disco#info for %s failed: %r", room, e)
        log.info("joined room %s", room)

    # ---------- 房间成员身份 ----------
    def _on_muc_presence(self, pres) -> None:
        room, nick = pres["from"].bare, pres["from"].resource
        if not nick:
            return
        if pres["type"] == "unavailable":
            self._occ.pop((room, nick), None)
            return
        el = pres.xml.find(f"{{{NS_OCC}}}occupant-id")
        if room in self._occ_rooms and el is not None and el.get("id"):
            self._occ[(room, nick)] = el.get("id")

    def occupant_ident(self, room: str, nick: str, stanza=None) -> str:
        """房间成员的稳定身份（映射成 OneBot user_id）：
        1. 能看到真实 JID（非匿名房间或 bot 是主持人）→ bare JID，跨房间、跨私聊一致；
        2. 房间声明支持 XEP-0421 → 服务器给的 occupant-id（按真实 JID 计算，别人换成同一昵称也拿不到）；
        3. 否则只能用 room/nick：昵称可以被别人在原主人离开后占用，管理员权限不要绑定在这种身份上。"""
        real = self.plugin["xep_0045"].get_jid_property(slixmpp.JID(room), nick, "jid")
        if real:
            return real.bare
        if room in self._occ_rooms:
            occ = None
            if stanza is not None:
                el = stanza.xml.find(f"{{{NS_OCC}}}occupant-id")
                occ = el.get("id") if el is not None else None
            occ = occ or self._occ.get((room, nick))
            if occ:
                return f"{OCC_PREFIX}{room}:{occ}"
        if room not in self._nick_rooms_warned:
            self._nick_rooms_warned.add(room)
            log.warning("room %s hides real JIDs and has no occupant-id: member identities are nickname-based "
                        "and a nickname can be taken over by someone else", room)
        return f"{room}/{nick}"

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

    def _trusted(self, jid: str) -> bool:
        """admins 或花名册中有订阅关系的联系人。"""
        if jid in self.cfg.admins:
            return True
        return jid in self.client_roster and \
            self.client_roster[jid]["subscription"] in ("both", "from", "to")

    async def _on_invite(self, inv) -> None:
        policy = self.cfg.accept_invites
        if policy == "none":
            return
        direct = inv.xml.find("{jabber:x:conference}x") is not None
        room = inv["groupchat_invite"]["jid"] if direct else inv["from"].bare
        room = str(room).split("/")[0]
        if not room or room in self.rooms():
            return
        if direct:
            inviter = inv["from"].bare
        else:
            el = inv.xml.find("{http://jabber.org/protocol/muc#user}x/{http://jabber.org/protocol/muc#user}invite")
            inviter = slixmpp.JID(el.get("from", "")).bare if el is not None else ""
        if policy == "roster" and not self._trusted(inviter):
            log.info("ignoring invite to %s from untrusted %s", room, inviter or "unknown")
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
        if self._stopping:
            return
        async with self._in_lock:
            if not self._stopping:
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
        if self.omemo and msg["from"].user and self._omemo_plugin.is_encrypted(msg):
            if msg["from"].bare in self.rooms():
                log.info("ignoring OMEMO message via MUC %s (not supported)", msg["from"])
                return 0
            if f"{msg['from'].bare}|{self._msg_id(msg)}" in self._seen:
                return 0   # 同一条加密消息重复到达（离线存储 + MAM）：再解密会失败，直接跳过
            try:
                dec, dev = await self._omemo_plugin.decrypt_message(msg)
            except Exception as e:  # noqa: BLE001
                log.warning("OMEMO decrypt failed for message from %s: %r", msg["from"], e)
                if archived_ts is None:
                    await self._omemo_resync(msg)
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
        body = self._clip(body, sender)
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
        kind = media.kind_of(url)
        if url.startswith("aesgcm://"):
            if kind is None:
                return None
            try:
                path = await self._fetch_aesgcm(url)
            except Exception as e:  # noqa: BLE001
                log.warning("failed to fetch aesgcm media from %s: %r", msg["from"], e)
                return None
            uri = Path(path).resolve().as_uri()
            return [Segment(kind, {"file": uri, "url": uri})]
        oob = msg["oob"]["url"] if msg.xml.find("{jabber:x:oob}x") is not None else ""
        if url.startswith(("http://", "https://")) and oob == url and kind and await self._safe_link(url):
            return [Segment(kind, {"file": url, "url": url})]
        return None

    async def _safe_link(self, url: str) -> bool:
        """入站媒体链接会被 bot 框架（及其插件）去下载：内网/本机地址的链接只当文本传，防止借框架做 SSRF。"""
        if self.media_cfg.allow_private_hosts or await media.is_public_url(url):
            return True
        log.warning("media link to a non-public address passed on as text: %s", url)
        return False

    async def _fetch_aesgcm(self, url: str) -> str:
        src, frag = media.aesgcm_split(url, self.media_cfg.aesgcm_insecure_http)
        limit = self.media_cfg.max_size_mb * 1024 * 1024
        payload = await media.download(src, limit + 16, self.media_cfg.allow_private_hosts,
                                       self.media_cfg.http_timeout)
        data = media.aesgcm_decrypt(payload, frag)
        d = Path(self.media_cfg.dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{hashlib.sha256(data).hexdigest()[:24]}.{media.ext_of(src) or 'bin'}"
        path.write_bytes(data)
        if self.media_cfg.max_dir_mb:
            media.prune_dir(d, self.media_cfg.max_dir_mb * 1024 * 1024)
        return str(path)

    def _clip(self, body: str, sender: str) -> str:
        n = self.cfg.max_body_chars
        if n and len(body) > n:
            log.info("truncating %d-char message from %s", len(body), sender)
            return body[:n]
        return body

    def _on_groupchat(self, msg):
        if self._stopping:
            return
        t = asyncio.ensure_future(self._handle_group(msg))
        t.add_done_callback(_log_failure)

    async def _handle_group(self, msg, archived_ts: float | None = None, archive_id: str | None = None) -> int:
        if self.omemo and msg["mucnick"] != self.cfg.nick and self._omemo_plugin.is_encrypted(msg):
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
        if archived_ts is None and msg["from"].bare not in self.rooms():
            log.info("ignoring groupchat message from %s (not joined)", msg["from"].bare)
            return 0   # 防止伪造「群」：只认已加入的房间
        retract = msg.xml.find(f"{{{NS_RETRACT}}}retract")
        if retract is not None and retract.get("id") and msg["mucnick"] and msg["mucnick"] != self.cfg.nick:
            room, nick = msg["from"].bare, msg["mucnick"]
            return await self._emit(RecallEvent(
                scene=Scene.GROUP, self_jid=self.boundjid.bare,
                sender_jid=self.occupant_ident(room, nick, msg), room_jid=room,
                target_id=retract.get("id"), time=archived_ts or time.time(),
                archived=archived_ts is not None), room)
        if msg["mucnick"] == self.cfg.nick or not msg["body"] or not msg["mucnick"]:
            return 0  # 忽略自己的回显和房间本身的通知
        if archived_ts is None and msg.xml.find(NS_DELAY) is not None:
            return 0  # 加入时的历史回放：由 MAM 补拉统一处理
        room, nick = msg["from"].bare, msg["mucnick"]
        sender = self.occupant_ident(room, nick, msg)
        body, reply = self._take_reply(msg, msg["body"])
        body = self._clip(body, sender)
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
        kind = media.kind_of(url) if url else None
        if url and url == oob and url.startswith(("http://", "https://")) and kind and await self._safe_link(url):
            segments.append(Segment(kind, {"file": url, "url": url}))
        else:
            segments.append(Segment("text", {"text": body}))
        return await self._emit(MessageEvent(
            scene=Scene.GROUP, self_jid=self.boundjid.bare, sender_jid=sender,
            room_jid=room, message_id=archive_id or self._msg_id(msg), segments=segments,
            time=archived_ts or time.time(), archived=archived_ts is not None,
            sender_name=nick,
            sender_role=self.plugin["xep_0045"].get_jid_property(room, nick, "affiliation") or "none",
        ), f"{room}")

    async def _omemo_resync(self, msg) -> None:
        """对方用我们已没有（或已不同步）的会话加密：重建会话（发出新的密钥交换），
        让对方客户端下一条消息改用新会话，并用加密消息提示对方重发。同一设备 5 分钟内只做一次。"""
        sender = msg["from"].bare
        sid = None
        for ns in ("urn:xmpp:omemo:2", "eu.siacs.conversations.axolotl"):
            h = msg.xml.find(f"{{{ns}}}encrypted/{{{ns}}}header")
            if h is not None and (h.get("sid") or "").isdigit():
                sid = int(h.get("sid"))
                break
        if sid is None:
            return
        now = time.monotonic()
        if now - self._resynced.get((sender, sid), -1e9) < (self.omemo_cfg.resync_cooldown if self.omemo_cfg else 300):
            return
        self._resynced[(sender, sid)] = now
        try:
            sm = await self._omemo_plugin.get_session_manager()
            dev = next((d for d in await sm.get_device_information(sender) if d.device_id == sid), None)
            if dev is None:
                await sm.refresh_device_lists(sender)
                dev = next((d for d in await sm.get_device_information(sender) if d.device_id == sid), None)
            if dev is None:
                log.warning("OMEMO: device %s of %s is unknown; cannot rebuild the session", sid, sender)
                return
            errors = await sm.replace_sessions(dev)
            if errors:
                log.warning("OMEMO: rebuilding sessions with %s/%s partly failed: %s", sender, sid, errors)
            log.info("OMEMO: rebuilt session with device %s of %s", sid, sender)
            notice = self.omemo_cfg.resync_notice if self.omemo_cfg else ""
            if notice:
                self._set_omemo_peer(sender, True)   # 提示必须加密发送
                await self.send_rich(sender, notice, group=False)
        except Exception as e:  # noqa: BLE001
            log.warning("OMEMO: failed to rebuild session with %s/%s: %r", sender, sid, e)

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
        """发送文本（可附带 OOB 链接），返回出站消息 id（同时作为 origin-id）。
        reply = (被回复者 JID, 被回复消息 id, 正文开头回退引用的长度)。
        对方最近用 OMEMO 发来消息时加密回复；加密失败直接报错，不降级为明文。"""
        msg = self.make_message(mto=to, mbody=text, mtype="groupchat" if group else "chat")
        mid = uuid.uuid4().hex
        msg["id"] = mid
        if self.is_encrypted_peer(to, group):
            from slixmpp.jid import JID
            xep = self._omemo_plugin
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
                                       input_file=io.BytesIO(payload), timeout=self.media_cfg.http_timeout)
            return media.aesgcm_url(url, frag)
        return await up.upload_file(filename, size=len(data), content_type=content_type,
                                    input_file=io.BytesIO(data), timeout=self.media_cfg.http_timeout)

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
        """把用户身份（真实 JID、occupant-id 或 room/nick）换成房间内当前昵称。"""
        if user.startswith(room + "/"):
            return user.split("/", 1)[1]
        if user.startswith(f"{OCC_PREFIX}{room}:"):
            occ = user[len(f"{OCC_PREFIX}{room}:"):]
            return next((n for (r, n), o in self._occ.items() if r == room and o == occ), None)
        muc = self.plugin["xep_0045"]
        room_j = slixmpp.JID(room)
        for nick in muc.get_roster(room_j) or []:
            j = muc.get_jid_property(room_j, nick, "jid")
            if j and j.bare == user:
                return nick
        return None
