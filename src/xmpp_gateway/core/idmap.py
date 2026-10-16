"""JID / 房间 / 消息 ID <-> int64 映射。OneBot v11 强制数字 ID，AstrBot 依赖它。"""
import os
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS ids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,          -- user | group | message
  value TEXT NOT NULL,
  UNIQUE(kind, value)
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
