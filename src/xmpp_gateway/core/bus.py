"""极简事件总线：XMPP 层 publish，协议出口 subscribe。"""
import asyncio


class EventBus:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def publish(self, event) -> None:
        for q in self._subs:
            q.put_nowait(event)
