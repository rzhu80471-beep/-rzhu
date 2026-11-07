"""异步事件总线（设计参考 vnpy 的 EventEngine，改为 asyncio 单线程模型）。

两类订阅者：
1. 同步处理器 `on(topic, handler)`：在发布时按注册顺序立即调用，用于撮合、K 线合成、
   策略回调这类必须有序、低延迟的链路。处理器异常会被捕获并记录，不会拖垮整条链路。
2. 异步队列 `stream(topics)`：用于 WebSocket 推送、数据库落盘等「慢消费者」。
   每个队列有上限，满了就丢弃最旧的消息（背压保护），慢客户端不会拖慢行情。

数据源可能在别的线程里回调（例如 miniQMT 的 xtdata），请用 `publish_threadsafe`。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# 事件主题
TICK = "tick"
BAR = "bar"
ORDER = "order"
TRADE = "trade"
ACCOUNT = "account"
POSITION = "position"
LOG = "log"
STATUS = "status"

Handler = Callable[[Any], None]


@dataclass(slots=True)
class Event:
    topic: str
    data: Any


@dataclass
class Subscription:
    topics: set[str] | None
    queue: asyncio.Queue[Event]
    dropped: int = 0
    _bus: "EventBus | None" = field(default=None, repr=False)

    def offer(self, event: Event) -> None:
        if self.topics is not None and event.topic not in self.topics:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()  # 丢最旧
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
            self.queue.put_nowait(event)

    async def get(self) -> Event:
        return await self.queue.get()

    def close(self) -> None:
        if self._bus is not None:
            self._bus.unsubscribe(self)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._subs: list[Subscription] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.counters: dict[str, int] = defaultdict(int)

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()

    def on(self, topic: str, handler: Handler) -> None:
        self._handlers[topic].append(handler)

    def off(self, topic: str, handler: Handler) -> None:
        if handler in self._handlers.get(topic, []):
            self._handlers[topic].remove(handler)

    def stream(self, topics: Iterable[str] | None = None, maxsize: int = 2000) -> Subscription:
        sub = Subscription(set(topics) if topics else None, asyncio.Queue(maxsize=maxsize), _bus=self)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, topic: str, data: Any) -> None:
        self.counters[topic] += 1
        for handler in list(self._handlers.get(topic, ())):
            try:
                handler(data)
            except Exception:  # noqa: BLE001
                log.exception("事件处理器出错 topic=%s handler=%s", topic, getattr(handler, "__qualname__", handler))
        if self._subs:
            event = Event(topic, data)
            for sub in list(self._subs):
                sub.offer(event)

    def publish_threadsafe(self, topic: str, data: Any) -> None:
        if self._loop is None:
            raise RuntimeError("EventBus 尚未绑定事件循环，先调用 bind_loop()")
        self._loop.call_soon_threadsafe(self.publish, topic, data)

    def log(self, message: str, level: str = "info", source: str = "system") -> None:
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")("[%s] %s", source, message)
        self.publish(LOG, {"level": level, "source": source, "message": message})
