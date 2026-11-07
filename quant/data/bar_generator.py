"""行情快照 -> 1 分钟 K 线 -> N 分钟 K 线（思路同 vnpy 的 BarGenerator）。

关键细节：
- 快照里的成交量是「当日累计」，分钟成交量要用差分；累计量变小说明换日或数据源重置
- K 线在「下一分钟的第一笔行情到来」时收盘；如果长时间没有新行情（午休、收盘、停牌），
  由引擎定时调用 flush(now) 按事件时间强制收盘
- 时间归属规则见 calendar.bar_minute（集合竞价并入 9:30，11:30/15:00 并入前一分钟）
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from ..core.calendar import bar_minute, minutes_since_open
from ..core.models import Bar, Tick

BarCallback = Callable[[Bar], None]


class BarGenerator:
    def __init__(self, on_bar: BarCallback, grace_seconds: float = 2.0):
        self.on_bar = on_bar
        self.grace = timedelta(seconds=grace_seconds)
        self._bars: dict[str, Bar] = {}
        self._last_volume: dict[str, int] = {}
        self._last_amount: dict[str, float] = {}
        self._last_date: dict[str, object] = {}

    def update_tick(self, tick: Tick) -> None:
        minute = bar_minute(tick.dt)
        sym = tick.symbol
        first_seen = sym not in self._last_date
        # 成交量差分
        if self._last_date.get(sym) != tick.dt.date():
            self._last_volume[sym] = 0
            self._last_amount[sym] = 0.0
            self._last_date[sym] = tick.dt.date()
        prev_v = self._last_volume.get(sym, 0)
        prev_a = self._last_amount.get(sym, 0.0)
        if tick.volume < prev_v:  # 数据源重置
            prev_v, prev_a = 0, 0.0
        dv = tick.volume - prev_v
        da = max(tick.amount - prev_a, 0.0)
        self._last_volume[sym] = tick.volume
        self._last_amount[sym] = tick.amount
        if first_seen and minute is not None and (minute.hour, minute.minute) != (9, 30):
            dv, da = 0, 0.0  # 盘中启动：第一笔的累计量属于之前的时段，不计入当前 K 线

        if minute is None:
            return
        bar = self._bars.get(sym)
        if bar is not None and bar.dt != minute:
            if minute < bar.dt:
                return  # 乱序的旧行情，丢弃
            self._finish(sym)
            bar = None
        if bar is None:
            self._bars[sym] = Bar(sym, minute, "1m", tick.last, tick.last, tick.last, tick.last, dv, da, tick.pre_close)
        else:
            bar.high = max(bar.high, tick.last)
            bar.low = min(bar.low, tick.last)
            bar.close = tick.last
            bar.volume += dv
            bar.amount += da

    def flush(self, now: datetime) -> None:
        """事件时间超过 K 线结束时间 + 宽限期，则强制收盘。"""
        for sym, bar in list(self._bars.items()):
            if now >= bar.dt + timedelta(minutes=1) + self.grace:
                self._finish(sym)

    def flush_all(self) -> None:
        for sym in list(self._bars):
            self._finish(sym)

    def _finish(self, sym: str) -> None:
        bar = self._bars.pop(sym, None)
        if bar is not None:
            bar.amount = round(bar.amount, 2)
            self.on_bar(bar)

    def current(self, sym: str) -> Bar | None:
        return self._bars.get(sym)


class WindowBarGenerator:
    """把 1 分钟 K 线合成为 N 分钟 K 线（按自 9:30 起的交易分钟对齐，午休不计）。"""

    def __init__(self, window: int, on_bar: BarCallback):
        if window < 2:
            raise ValueError("window 至少为 2")
        self.window = window
        self.on_bar = on_bar
        self._bars: dict[str, Bar] = {}
        self._start_idx: dict[str, int] = {}

    def update_bar(self, bar: Bar) -> None:
        idx = minutes_since_open(bar.dt) // self.window
        cur = self._bars.get(bar.symbol)
        if cur is not None and (self._start_idx[bar.symbol] != idx or cur.dt.date() != bar.dt.date()):
            self.on_bar(self._bars.pop(bar.symbol))
            cur = None
        if cur is None:
            self._bars[bar.symbol] = Bar(bar.symbol, bar.dt, f"{self.window}m", bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount, bar.pre_close)
            self._start_idx[bar.symbol] = idx
        else:
            cur.high = max(cur.high, bar.high)
            cur.low = min(cur.low, bar.low)
            cur.close = bar.close
            cur.volume += bar.volume
            cur.amount += bar.amount
        # 窗口最后一分钟到达即收盘，不必等下一根
        if (minutes_since_open(bar.dt) + 1) % self.window == 0 or minutes_since_open(bar.dt) == 239:
            self.on_bar(self._bars.pop(bar.symbol))
