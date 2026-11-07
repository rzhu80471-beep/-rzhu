from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from ..core.calendar import now_cn
from ..core.events import STATUS, TICK, EventBus
from ..core.models import Tick


class DataFeed(ABC):
    """行情源基类。子类只需实现 run()，产出的 Tick 通过 emit() 发布到事件总线。"""

    source = "base"

    def __init__(self, bus: EventBus, symbols: list[str]):
        self.bus = bus
        self.symbols: list[str] = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        self._running = False
        self._stop_event = asyncio.Event()
        self.state = "idle"  # idle / running / closed / rate_limited / error
        self.stats: dict[str, Any] = {"polls": 0, "ticks": 0, "errors": 0, "latency_ms": 0.0, "last_tick": None}

    # 事件时间。仿真源会覆盖为加速时钟。
    def now(self) -> datetime:
        return now_cn()

    def add_symbol(self, symbol: str) -> None:
        if symbol not in self.symbols:
            self.symbols.append(symbol)

    def remove_symbol(self, symbol: str) -> None:
        if symbol in self.symbols:
            self.symbols.remove(symbol)

    def emit(self, tick: Tick) -> None:
        self.stats["ticks"] += 1
        self.stats["last_tick"] = tick.dt.isoformat(timespec="seconds")
        self.bus.publish(TICK, tick)

    def set_state(self, state: str, message: str = "") -> None:
        if state != self.state:
            self.state = state
            self.bus.publish(STATUS, {"feed": self.source, "state": state, "message": message})
            if message:
                self.bus.log(message, "warning" if state in ("rate_limited", "error") else "info", self.source)

    async def sleep(self, seconds: float) -> None:
        """可被 stop() 打断的 sleep。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(seconds, 0))
        except asyncio.TimeoutError:
            pass

    @abstractmethod
    async def run(self) -> None: ...

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        self.state = "idle"

    def status(self) -> dict[str, Any]:
        return {"source": self.source, "state": self.state, "symbols": list(self.symbols), "now": self.now().isoformat(timespec="seconds"), **self.stats}
