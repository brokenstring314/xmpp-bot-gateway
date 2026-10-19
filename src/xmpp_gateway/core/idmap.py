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
CREATE TABLE IF NOT EXISTS state (     -- 杂项持久状态（如 MAM 补拉检查点）
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
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
    def __init__(self, path: str, keep_days: int = 30) -> None:
        self.path = path
        self.keep_days = keep_days
        self.db: aiosqlite.Connection | None = None
        self._saves = 0

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        await self.prune()

    async def prune(self) -> None:
        """删除超过 keep_days 的消息记录（及其 ID 映射和别名），防止数据库无限增长。"""
        if not self.keep_days or not self.db:
            return
        cutoff = int(time.time()) - self.keep_days * 86400
        await self.db.execute("DELETE FROM ids WHERE kind='message' AND id IN (SELECT id FROM messages WHERE time < ?)",
                              (cutoff,))
        await self.db.execute("DELETE FROM messages WHERE time < ?", (cutoff,))
        await self.db.execute("DELETE FROM msg_alias WHERE message_id - ? NOT IN (SELECT id FROM messages)", (BASE,))
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
            row = await cur.fetchone()
        assert row is not None
        return BASE + row[0]

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
        self._saves += 1
        if self._saves % 1000 == 0:
            await self.prune()
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

    async def get_state(self, key: str) -> str | None:
        assert self.db
        async with self.db.execute("SELECT value FROM state WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_state(self, key: str, value: str) -> None:
        assert self.db
        await self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))
        await self.db.commit()

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
        d = dict(zip(keys, row, strict=True))
        d["segments"] = json.loads(d["segments"])
        d["outgoing"] = bool(d["outgoing"])
        return d
