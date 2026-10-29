"""OneBot v11 出口：反向 WebSocket 客户端（Universal 角色），对接 AstrBot / NoneBot 等。

- 事件：message.private / message.group，meta_event.lifecycle(connect) 与 heartbeat
- 动作：见 Actions.TABLE
- 断线后自动重连；断线期间积压的过期事件会被丢弃（event_max_age）
"""
import asyncio
import json
import logging
import time

import aiohttp

from ... import __version__
from ...core.events import RecallEvent, Scene
from ...xmpp import media
from . import cq

log = logging.getLogger(__name__)


def ok(data=None) -> dict:
    return {"status": "ok", "retcode": 0, "data": data}


def failed(retcode: int, wording: str) -> dict:
    return {"status": "failed", "retcode": retcode, "data": None, "msg": wording, "wording": wording}


class ActionError(Exception):
    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(wording)
        self.retcode = retcode


ROLE = {"owner": "owner", "admin": "admin"}


class OneBotV11:
    def __init__(self, cfg, bus, idmap, xmpp) -> None:
        self.cfg, self.bus, self.idmap, self.xmpp = cfg, bus, idmap, xmpp
        self.self_id = 0

    async def run(self) -> None:
        self.self_id = await self.idmap.to_int("user", self.xmpp.boundjid.bare)
        if not self.cfg.reverse_ws:
            log.warning("OneBot v11 enabled but no reverse_ws configured")
        await asyncio.gather(*(self._reverse_ws(ep.url, self.cfg.access_token if ep.access_token is None
                                                else ep.access_token) for ep in self.cfg.reverse_ws))

    # ---------- 连接管理 ----------
    async def _reverse_ws(self, url: str, token: str) -> None:
        headers = {"X-Self-ID": str(self.self_id), "X-Client-Role": "Universal",
                   "User-Agent": f"xmpp-bot-gateway/{__version__}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        events = self.bus.subscribe()
        carry: list = []
        delay = 1
        while True:
            try:
                async with aiohttp.ClientSession() as s, s.ws_connect(url, headers=headers, heartbeat=30) as ws:
                    log.info("OneBot v11 reverse WS connected: %s", url)
                    delay = 1
                    lock = asyncio.Lock()
                    send = self._sender(ws, lock)
                    await send(self._meta("lifecycle", sub_type="connect"))
                    tasks = [asyncio.create_task(c) for c in
                             (self._pump(send, events, carry), self._recv(ws, send), self._heartbeat(send))]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    # 读出所有已结束任务的异常（否则 asyncio 会报 "exception was never retrieved"）
                    excs = [e for t in done if not t.cancelled() and (e := t.exception()) is not None]
                    if excs:
                        raise excs[0]
                    log.warning("reverse WS %s closed by peer", url)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("reverse WS %s error: %s", url, e)
            log.info("reconnecting to %s in %ss", url, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.cfg.reconnect_max)

    @staticmethod
    def _sender(ws, lock):
        async def send(obj: dict) -> None:
            async with lock:
                await ws.send_str(json.dumps(obj, ensure_ascii=False))
        return send

    def _meta(self, kind: str, **extra) -> dict:
        return {"time": int(time.time()), "self_id": self.self_id, "post_type": "meta_event",
                "meta_event_type": kind, **extra}

    def _status(self) -> dict:
        return {"online": bool(self.xmpp.online), "good": bool(self.xmpp.online)}

    async def _heartbeat(self, send) -> None:
        interval = self.cfg.heartbeat_interval
        if interval <= 0:
            await asyncio.Event().wait()
        while True:
            await asyncio.sleep(interval / 1000)
            try:
                await send(self._meta("heartbeat", status=self._status(), interval=interval))
            except (ConnectionError, aiohttp.ClientError):
                return   # 连接正在关闭：由接收循环负责报告与重连

    async def _pump(self, send, events: asyncio.Queue, carry: list) -> None:
        """carry：上一条连接上没发出去的事件，换新连接后先补发，避免重连瞬间丢消息。"""
        while True:
            ev = carry.pop(0) if carry else await events.get()
            if self.cfg.event_max_age and not ev.archived and time.time() - ev.time > self.cfg.event_max_age:
                log.info("dropping stale event %s (%.0fs old)", ev.message_id, time.time() - ev.time)
                continue
            try:
                payload = await (self._translate_recall(ev) if isinstance(ev, RecallEvent) else self._translate(ev))
                if payload is None:
                    continue
            except Exception:  # noqa: BLE001
                log.exception("failed to translate event")
                continue
            try:
                await send(payload)
            except BaseException:   # 含连接关闭时的取消
                carry.insert(0, ev)
                raise

    async def _recv(self, ws, send) -> None:
        async for m in ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                req = json.loads(m.data)
            except json.JSONDecodeError:
                continue
            asyncio.create_task(self._serve(req, send))

    async def _serve(self, req: dict, send) -> None:
        action = str(req.get("action", "")).removesuffix("_async")
        try:
            resp = await self.call(action, req.get("params") or {})
        except ActionError as e:
            resp = failed(e.retcode, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("action %s failed", action)
            resp = failed(100, f"internal error: {e}")
        if "echo" in req:
            resp["echo"] = req["echo"]
        await send(resp)

    # ---------- 事件翻译 ----------
    async def _translate_recall(self, ev) -> dict | None:
        """对方撤回 -> friend_recall / group_recall 通知。只认撤回者自己发的、本网关见过的消息。"""
        group = ev.scene == Scene.GROUP
        chat = ev.room_jid if group else ev.sender_jid
        mid = await self.idmap.find_message(chat, ev.target_id)
        rec = await self.idmap.get_message(mid) if mid else None
        if not rec or rec["outgoing"] or rec["sender"] != ev.sender_jid:
            log.info("ignoring retraction of unknown or foreign message %s in %s", ev.target_id, chat)
            return None
        uid = await self.idmap.to_int("user", ev.sender_jid)
        base = {"time": int(ev.time or time.time()), "self_id": self.self_id, "post_type": "notice",
                "user_id": uid, "message_id": mid}
        if group:
            return base | {"notice_type": "group_recall", "operator_id": uid,
                           "group_id": await self.idmap.to_int("group", ev.room_jid)}
        return base | {"notice_type": "friend_recall"}

    async def _translate(self, ev) -> dict:
        segs = []
        chat = ev.room_jid if ev.scene == Scene.GROUP else ev.sender_jid
        for s in ev.segments:
            if s.type == "reply":
                xid = s.data["xmpp_id"]
                rid = await self.idmap.find_message(chat, xid)
                if rid is None and ev.scene == Scene.GROUP:   # 回复的是本 bot 在群里发的消息
                    oid = self.xmpp.origin_of_reflection(chat, xid)
                    rid = await self.idmap.find_message(chat, oid) if oid else None
                if rid is not None:
                    segs.append({"type": "reply", "data": {"id": str(rid)}})
                elif s.data.get("fallback"):
                    segs.append({"type": "text", "data": {"text": s.data["fallback"]}})
            elif s.type == "text":
                segs.append({"type": "text", "data": {"text": s.data.get("text", "")}})
            elif s.type in ("image", "record", "video"):
                segs.append({"type": s.type, "data": dict(s.data)})
            elif s.type == "at":
                segs.append({"type": "at", "data": {"qq": str(await self.idmap.to_int("user", s.data["target"]))}})
        uid = await self.idmap.to_int("user", ev.sender_jid)
        group = ev.scene == Scene.GROUP
        mid = await self.idmap.save_message(
            scene=ev.scene.value, chat=ev.room_jid if group else ev.sender_jid, sender=ev.sender_jid,
            sender_name=ev.sender_name, xmpp_id=ev.message_id, outgoing=False, segments=segs, ts=ev.time)
        raw = cq.to_cq(segs)
        base = {
            "time": int(ev.time or time.time()), "self_id": self.self_id, "post_type": "message",
            "message_id": mid, "user_id": uid, "font": 0,
            "message": segs if self.cfg.message_format == "array" else raw, "raw_message": raw,
        }
        if group:
            base |= {"message_type": "group", "sub_type": "normal", "anonymous": None,
                     "group_id": await self.idmap.to_int("group", ev.room_jid),
                     "sender": {"user_id": uid, "nickname": ev.sender_name, "card": ev.sender_name,
                                "role": ROLE.get(ev.sender_role, "member")}}
        else:
            in_roster = ev.sender_jid in self.xmpp.client_roster
            base |= {"message_type": "private", "sub_type": "friend" if in_roster else "other",
                     "sender": {"user_id": uid, "nickname": ev.sender_name}}
        return base

    # ---------- 动作 ----------
    async def _jid(self, kind: str, num) -> str:
        try:
            n = int(num)
        except (TypeError, ValueError):
            raise ActionError(100, f"invalid {kind} id: {num!r}") from None
        v = await self.idmap.to_str(kind, n)
        if not v:
            raise ActionError(100, f"unknown {kind} id: {n}")
        return v

    def _render(self, segments: list[dict], room: str | None, nicks: dict[int, str]) -> tuple[str, list[dict], list]:
        """消息段 -> (文字正文, 媒体段列表, 提及列表)。媒体每个单独发一条消息，客户端才会内联显示。
        提及为 (begin, end, uri)，供 XEP-0372 引用。"""
        text, urls, mentions = [], [], []
        for i, s in enumerate(segments):
            t, d = s.get("type"), s.get("data") or {}
            if t == "text":
                text.append(str(d.get("text", "")))
            elif t == "at" and room:
                qq = str(d.get("qq", ""))
                nick = "all" if qq == "all" else None
                if nick is None and qq.isdigit():
                    nick = nicks.get(int(qq))
                if nick:
                    pos = sum(len(t) for t in text)
                    if nick != "all":
                        mentions.append((pos, pos + len(nick), f"xmpp:{room}/{nick}"))
                    text.append(f"{nick}: " if i == 0 else f"{nick} ")
            elif t in ("image", "record", "video", "file"):
                urls.append(s)
            elif t == "reply":
                pass  # 由 _reply_ref 处理
            else:
                log.debug("unsupported segment %s skipped", t)
        joined = "".join(text)
        body = joined.strip("\n")
        lead = len(joined) - len(joined.lstrip("\n"))
        mentions = [(b - lead, e - lead, u) for b, e, u in mentions if e - lead <= len(body)]
        return body, urls, mentions

    async def _media_url(self, seg: dict, encrypt: bool) -> str:
        """媒体段 -> 可发送的链接：http(s) 原样使用；base64:// / file:// / 本地路径经 HTTP Upload 上传。"""
        d = seg.get("data") or {}
        src = str(d.get("file") or d.get("url") or "")
        if not src.startswith(("http://", "https://")) and str(d.get("url", "")).startswith(("http://", "https://")):
            src = str(d["url"])
        if src.startswith(("http://", "https://")):
            return src
        if not src:
            raise ActionError(100, f"{seg.get('type')} segment has no file")
        limit = self.xmpp.media_cfg.max_size_mb * 1024 * 1024
        try:
            data, name, mime = media.load_source(src, d.get("name"), limit, self.xmpp.media_cfg.local_file_dirs)
        except (OSError, ValueError) as e:
            raise ActionError(100, f"cannot read {seg.get('type')}: {e}") from e
        try:
            return await self.xmpp.upload(data, name, mime, encrypt)
        except Exception as e:  # noqa: BLE001
            raise ActionError(100, f"upload failed: {e!r}") from e

    async def _send(self, group: bool, target_num, message, auto_escape=False) -> dict:
        kind = "group" if group else "user"
        target = await self._jid(kind, target_num)
        if not group and target.startswith("occupant-id:"):   # 匿名房间成员：发到其当前昵称（MUC 私信）
            room_of = target[len("occupant-id:"):].rsplit(":", 1)[0]
            nick = self.xmpp.nick_for(room_of, target)
            if not nick:
                raise ActionError(100, "that room member is not in the room now")
            target = f"{room_of}/{nick}"
        segments = cq.normalize(message, bool(auto_escape))
        room = target if group else None
        if group and target not in self.xmpp.rooms():
            raise ActionError(100, f"not in room {target}")
        nicks: dict[int, str] = {}
        if room:
            for s in segments:
                qq = str((s.get("data") or {}).get("qq", ""))
                if s.get("type") == "at" and qq.isdigit():
                    user = await self.idmap.to_str("user", int(qq))
                    nick = self.xmpp.nick_for(room, user) if user else None
                    if nick:
                        nicks[int(qq)] = nick
        body, media_segs, mentions = self._render(segments, room, nicks)
        reply = await self._reply_ref(segments, target, group)
        if not body and not media_segs:
            raise ActionError(100, "empty message")
        if not self.xmpp.online:
            raise ActionError(100, "XMPP not connected")
        encrypt = self.xmpp.is_encrypted_peer(target, group)
        links = [await self._media_url(s, encrypt) for s in media_segs]  # 先全部上传，失败则整条不发
        ids = []
        try:
            if body:
                ref = (reply[0], reply[1], len(reply[2])) if reply else None
                shift = len(reply[2]) if reply else 0
                ids.append(await self.xmpp.send_rich(
                    target, reply[2] + body if reply else body, group, reply=ref,
                    mentions=[(b + shift, e + shift, u) for b, e, u in mentions]))
                reply = None
            for url in links:
                # 正文恰为链接（+ OOB）时 Conversations 等客户端会内联显示；
                # 媒体消息不加引用回退文本，否则正文不再是纯链接
                oob = [url] if url.startswith(("http://", "https://")) else None
                ref = (reply[0], reply[1], 0) if reply else None
                ids.append(await self.xmpp.send_rich(target, url, group, oob, reply=ref))
                reply = None
        except Exception as e:  # noqa: BLE001  如 OMEMO 加密失败
            raise ActionError(100, f"send failed: {e}") from e
        xid = ",".join(ids)
        mid = await self.idmap.save_message(
            scene="group" if group else "private", chat=target, sender=self.xmpp.boundjid.bare,
            sender_name=self.xmpp.cfg.nick, xmpp_id=xid, outgoing=True, segments=segments)
        return ok({"message_id": mid})

    async def _reply_ref(self, segments: list[dict], target: str, group: bool) -> tuple[str, str, str] | None:
        """reply 段 -> (被回复者 JID, 被回复消息的 XMPP id, 引用回退文本)。找不到原消息时忽略。"""
        seg = next((s for s in segments if s.get("type") == "reply"), None)
        if not seg:
            return None
        try:
            rec = await self.idmap.get_message(int((seg.get("data") or {}).get("id")))  # type: ignore[arg-type]  # None → TypeError
        except (TypeError, ValueError):
            rec = None
        if not rec or rec["chat"] != target:
            log.warning("reply target %s not found in this chat; sending without reply", seg.get("data"))
            return None
        xid = rec["xmpp_id"].split(",")[0]
        if group:
            nick = self.xmpp.cfg.nick if rec["outgoing"] else rec["sender_name"]
            to = f"{target}/{nick}"
            if rec["outgoing"]:
                xid = self.xmpp.room_id_of_own(target, xid)
        else:
            to = self.xmpp.boundjid.bare if rec["outgoing"] else rec["sender"]
        quoted = "".join(s["data"].get("text", "") for s in rec["segments"] if s.get("type") == "text")
        quoted = quoted.strip() or "[media]"
        if len(quoted) > 80:
            quoted = quoted[:80] + "…"
        name = rec["sender_name"] or rec["sender"].split("@")[0]
        fallback = "> " + f"{name}: " + "\n> ".join(quoted.splitlines()) + "\n"
        return to, xid, fallback

    async def call(self, action: str, p: dict) -> dict:
        fn = getattr(self, f"a_{action}", None)
        if not action or fn is None:
            return failed(1404, f"unsupported action: {action}")
        return await fn(p)

    async def a_send_private_msg(self, p):
        return await self._send(False, p.get("user_id"), p.get("message", ""), p.get("auto_escape"))

    async def a_send_group_msg(self, p):
        return await self._send(True, p.get("group_id"), p.get("message", ""), p.get("auto_escape"))

    async def a_send_msg(self, p):
        group = p.get("message_type") == "group" or (p.get("message_type") is None and p.get("group_id"))
        return await self._send(bool(group), p.get("group_id") if group else p.get("user_id"),
                                p.get("message", ""), p.get("auto_escape"))

    async def a_delete_msg(self, p):
        rec = await self.idmap.get_message(int(p.get("message_id", 0)))
        if not rec:
            raise ActionError(100, "unknown message_id")
        if not rec["outgoing"]:
            raise ActionError(100, "only messages sent by the bot can be retracted")
        self.xmpp.send_retract(rec["chat"], rec["xmpp_id"], rec["scene"] == "group")
        return ok()

    async def a_get_msg(self, p):
        mid = int(p.get("message_id", 0))
        rec = await self.idmap.get_message(mid)
        if not rec:
            raise ActionError(100, "unknown message_id")
        group = rec["scene"] == "group"
        segs = rec["segments"]
        data = {"time": rec["time"], "message_type": rec["scene"], "message_id": mid, "real_id": mid,
                "sender": {"user_id": await self.idmap.to_int("user", rec["sender"]),
                           "nickname": rec["sender_name"]},
                "message": segs if self.cfg.message_format == "array" else cq.to_cq(segs),
                "raw_message": cq.to_cq(segs)}
        if group:
            data["group_id"] = await self.idmap.to_int("group", rec["chat"])
        return ok(data)

    async def a_get_login_info(self, p):
        return ok({"user_id": self.self_id, "nickname": self.xmpp.cfg.nick})

    async def a_get_status(self, p):
        return ok(self._status())

    async def a_get_version_info(self, p):
        return ok({"app_name": "xmpp-bot-gateway", "app_version": __version__, "protocol_version": "v11"})

    async def a_can_send_image(self, p):
        return ok({"yes": True})

    async def a_can_send_record(self, p):
        return ok({"yes": True})

    async def a_get_friend_list(self, p):
        roster = self.xmpp.client_roster
        out = []
        for jid in roster:
            item = roster[jid]
            if jid == self.xmpp.boundjid.bare or item["subscription"] not in ("both", "to", "from"):
                continue
            out.append({"user_id": await self.idmap.to_int("user", jid),
                        "nickname": item["name"] or jid.split("@")[0], "remark": item["name"] or ""})
        return ok(out)

    async def a_get_stranger_info(self, p):
        jid = await self._jid("user", p.get("user_id"))
        if jid.startswith("occupant-id:"):
            room = jid[len("occupant-id:"):].rsplit(":", 1)[0]
            name = self.xmpp.nick_for(room, jid) or "unknown"
        else:
            name = jid.split("/", 1)[1] if "/" in jid else jid.split("@")[0]
        return ok({"user_id": int(p["user_id"]), "nickname": name, "sex": "unknown", "age": 0})

    async def _group_info(self, room: str) -> dict:
        name = room.split("@")[0]
        try:
            info = await self.xmpp.plugin["xep_0030"].get_info(jid=room, timeout=5)
            for _cat, _typ, _lang, n in info["disco_info"]["identities"]:
                if n:
                    name = n
                    break
        except Exception:  # noqa: BLE001
            pass
        count = len(self.xmpp.plugin["xep_0045"].get_roster(room) or [])
        return {"group_id": await self.idmap.to_int("group", room), "group_name": name,
                "member_count": count, "max_member_count": 0}

    async def a_get_group_list(self, p):
        return ok([await self._group_info(r) for r in self.xmpp.rooms()])

    async def a_get_group_info(self, p):
        room = await self._jid("group", p.get("group_id"))
        if room not in self.xmpp.rooms():
            raise ActionError(100, f"not in room {room}")
        return ok(await self._group_info(room))

    async def _member(self, room: str, gid: int, nick: str) -> dict:
        muc = self.xmpp.plugin["xep_0045"]
        ident = self.xmpp.occupant_ident(room, nick)
        aff = muc.get_jid_property(room, nick, "affiliation") or "none"
        return {"group_id": gid, "user_id": await self.idmap.to_int("user", ident), "nickname": nick,
                "card": nick, "sex": "unknown", "age": 0, "area": "", "join_time": 0,
                "last_sent_time": 0, "level": "", "role": ROLE.get(aff, "member"), "unfriendly": False,
                "title": "", "title_expire_time": 0, "card_changeable": False}

    async def a_get_group_member_list(self, p):
        room = await self._jid("group", p.get("group_id"))
        gid = int(p["group_id"])
        return ok([await self._member(room, gid, n) for n in self.xmpp.plugin["xep_0045"].get_roster(room) or []])

    async def a_get_group_member_info(self, p):
        room = await self._jid("group", p.get("group_id"))
        user = await self._jid("user", p.get("user_id"))
        nick = self.xmpp.nick_for(room, user)
        if not nick:
            raise ActionError(100, "user not in room")
        return ok(await self._member(room, int(p["group_id"]), nick))

    async def a_set_group_leave(self, p):
        room = await self._jid("group", p.get("group_id"))
        self.xmpp.plugin["xep_0045"].leave_muc(room, self.xmpp.cfg.nick)
        return ok()
