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
        await self.db.commit()
        return mid

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
