"""JID / 房间 / 消息 ID <-> int64 映射，以及消息记录（供 get_msg / delete_msg 使用）。

OneBot v11 强制数字 ID，AstrBot 等框架依赖它，因此映射必须持久化且稳定。
"""
import json
import os
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS ids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,          -- user | group | message
  value TEXT NOT NULL,
  UNIQUE(kind, value)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,      -- 对应 ids.id（kind=message）
  scene TEXT NOT NULL,         -- private | group
  chat TEXT NOT NULL,          -- 对端 JID 或房间 JID
  sender TEXT NOT NULL,
  sender_name TEXT NOT NULL DEFAULT '',
  xmpp_id TEXT NOT NULL,       -- stanza-id / origin-id
  outgoing INTEGER NOT NULL,
  segments TEXT NOT NULL,      -- JSON
  time INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS msg_alias ( -- 一条 OneBot 消息对应的其他 XMPP id（拆分发送的各段、群里的 stanza-id）
  chat TEXT NOT NULL,
  xmpp_id TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  PRIMARY KEY(chat, xmpp_id)
);
"""
BASE = 10_000_000  # 避开小数字，便于肉眼区分


class IdMap:
    def __init__(self, path: str) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def to_int(self, kind: str, value: str) -> int:
        assert self.db
        await self.db.execute("INSERT OR IGNORE INTO ids(kind, value) VALUES (?, ?)", (kind, value))
        await self.db.commit()
        async with self.db.execute("SELECT id FROM ids WHERE kind=? AND value=?", (kind, value)) as cur:
            (rid,) = await cur.fetchone()
        return BASE + rid

    async def to_str(self, kind: str, num: int) -> str | None:
        assert self.db
        async with self.db.execute("SELECT value FROM ids WHERE kind=? AND id=?", (kind, num - BASE)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def save_message(self, *, scene: str, chat: str, sender: str, sender_name: str,
                           xmpp_id: str, outgoing: bool, segments: list[dict], ts: float | None = None) -> int:
        """记录一条消息并返回其 OneBot message_id。key 带上会话，避免不同会话 id 冲突。"""
        assert self.db
        mid = await self.to_int("message", f"{chat}|{xmpp_id}")
        await self.db.execute(
            "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            (mid - BASE, scene, chat, sender, sender_name, xmpp_id, int(outgoing),
             json.dumps(segments, ensure_ascii=False), int(ts or time.time())))
        if "," in xmpp_id:
            for part in xmpp_id.split(","):
                await self.db.execute("INSERT OR REPLACE INTO msg_alias VALUES (?,?,?)", (chat, part, mid))
        await self.db.commit()
        return mid

    async def find_message(self, chat: str, xmpp_id: str) -> int | None:
        """按会话 + XMPP id（含别名）查 OneBot message_id，不存在返回 None。"""
        assert self.db
        async with self.db.execute("SELECT m.id FROM ids i JOIN messages m ON m.id = i.id "
                                   "WHERE i.kind='message' AND i.value=?", (f"{chat}|{xmpp_id}",)) as cur:
            row = await cur.fetchone()
        if row:
            return BASE + row[0]
        async with self.db.execute("SELECT message_id FROM msg_alias WHERE chat=? AND xmpp_id=?",
                                   (chat, xmpp_id)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def add_alias(self, chat: str, xmpp_id: str, mid: int) -> None:
        assert self.db
        await self.db.execute("INSERT OR REPLACE INTO msg_alias VALUES (?,?,?)", (chat, xmpp_id, mid))
        await self.db.commit()

    async def get_message(self, mid: int) -> dict | None:
        assert self.db
        async with self.db.execute("SELECT scene, chat, sender, sender_name, xmpp_id, outgoing, segments, time "
                                   "FROM messages WHERE id=?", (mid - BASE,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        keys = ("scene", "chat", "sender", "sender_name", "xmpp_id", "outgoing", "segments", "time")
        d = dict(zip(keys, row))
        d["segments"] = json.loads(d["segments"])
        d["outgoing"] = bool(d["outgoing"])
        return d
