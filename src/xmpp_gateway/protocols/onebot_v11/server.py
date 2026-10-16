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
from ...core.events import Scene
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
        await asyncio.gather(*(self._reverse_ws(url) for url in self.cfg.reverse_ws))

    # ---------- 连接管理 ----------
    async def _reverse_ws(self, url: str) -> None:
        headers = {"X-Self-ID": str(self.self_id), "X-Client-Role": "Universal",
                   "User-Agent": f"xmpp-bot-gateway/{__version__}"}
        if self.cfg.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.access_token}"
        events = self.bus.subscribe()
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
                             (self._pump(send, events), self._recv(ws, send), self._heartbeat(send))]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()
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
            await send(self._meta("heartbeat", status=self._status(), interval=interval))

    async def _pump(self, send, events: asyncio.Queue) -> None:
        while True:
            ev = await events.get()
            if self.cfg.event_max_age and time.time() - ev.time > self.cfg.event_max_age:
                log.info("dropping stale event %s (%.0fs old)", ev.message_id, time.time() - ev.time)
                continue
            try:
                payload = await self._translate(ev)
            except Exception:  # noqa: BLE001
                log.exception("failed to translate event")
                continue
            await send(payload)

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
    async def _translate(self, ev) -> dict:
        segs = []
        for s in ev.segments:
            if s.type == "text":
                segs.append({"type": "text", "data": {"text": s.data.get("text", "")}})
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

    def _render(self, segments: list[dict], room: str | None, nicks: dict[int, str]) -> tuple[str, list[str]]:
        """消息段 -> (XMPP 正文, OOB 链接)。P0 只支持 http(s) 链接形式的媒体。"""
        text, urls = [], []
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
                    text.append(f"{nick}: " if i == 0 else f"{nick} ")
            elif t in ("image", "record", "video", "file"):
                url = str(d.get("url") or d.get("file") or "")
                if url.startswith(("http://", "https://")):
                    urls.append(url)
                else:
                    log.warning("%s segment without http(s) url not supported yet; skipped", t)
            elif t == "reply":
                pass  # P2：XEP-0461
            else:
                log.debug("unsupported segment %s skipped", t)
        body = "".join(text).strip("\n")
        if urls:
            # 正文恰为 URL 时 Conversations 等客户端会内联显示图片
            body = "\n".join(filter(None, [body, *urls]))
        return body, urls

    async def _send(self, group: bool, target_num, message, auto_escape=False) -> dict:
        kind = "group" if group else "user"
        target = await self._jid(kind, target_num)
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
        body, urls = self._render(segments, room, nicks)
        if not body:
            raise ActionError(100, "empty message")
        if not self.xmpp.online:
            raise ActionError(100, "XMPP not connected")
        xid = self.xmpp.send_rich(target, body, group, urls)
        mid = await self.idmap.save_message(
            scene="group" if group else "private", chat=target, sender=self.xmpp.boundjid.bare,
            sender_name=self.xmpp.cfg.nick, xmpp_id=xid, outgoing=True, segments=segments)
        return ok({"message_id": mid})

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
        return ok({"yes": False})

    async def a_can_send_record(self, p):
        return ok({"yes": False})

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
        real = muc.get_jid_property(room, nick, "jid")
        ident = real.bare if real else f"{room}/{nick}"
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
