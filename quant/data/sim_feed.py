"""仿真行情源：在本地生成符合 A 股规则的逐笔快照，用于收盘后/周末联调、压测和演示。

- 加速时钟：speed=30 表示真实 1 秒 = 行情 30 秒，2 秒走完一根 1 分钟 K 线
- 自动跳过午休，15:00 收盘后进入下一个交易日（昨收=今收，成交量清零），可以测试 T+1
- 价格为带「趋势段」的几何布朗运动，严格限制在涨跌停之间；涨停时卖盘为空、跌停时买盘为空
"""
from __future__ import annotations

import math
import random
from datetime import datetime, time, timedelta
from typing import Any

from ..core.calendar import AFTERNOON_CLOSE, AFTERNOON_OPEN, MORNING_CLOSE, MORNING_OPEN, minutes_since_open, now_cn
from ..core.events import EventBus
from ..core.models import Tick
from ..core.rules import limit_prices, round_price
from .base import DataFeed

DEFAULT_UNIVERSE: dict[str, tuple[str, float]] = {
    "600000": ("浦发银行", 10.50),
    "000001": ("平安银行", 11.20),
    "600519": ("贵州茅台", 1500.0),
    "300750": ("宁德时代", 250.0),
    "688981": ("中芯国际", 90.0),
    "002594": ("比亚迪", 300.0),
}


class _SimState:
    __slots__ = ("symbol", "name", "pre_close", "last", "open", "high", "low", "volume", "amount", "drift", "drift_left")

    def __init__(self, symbol: str, name: str, price: float):
        self.symbol, self.name = symbol, name
        self.pre_close = price
        self.reset_day(price)
        self.drift = 0.0
        self.drift_left = 0

    def reset_day(self, pre_close: float) -> None:
        self.pre_close = pre_close
        self.last = self.open = self.high = self.low = 0.0
        self.volume = 0
        self.amount = 0.0


class SimFeed(DataFeed):
    source = "sim"

    def __init__(
        self,
        bus: EventBus,
        symbols: list[str],
        speed: float = 30.0,
        tick_interval: float = 0.5,
        daily_vol: float = 0.025,
        seed: int | None = None,
        start: datetime | None = None,
        prices: dict[str, float] | None = None,
        names: dict[str, str] | None = None,
    ):
        super().__init__(bus, symbols)
        self.speed = speed
        self.tick_interval = tick_interval
        self.daily_vol = daily_vol
        self.rng = random.Random(seed)
        today = (start or now_cn()).date()
        self._clock = start or datetime.combine(today, time(9, 25))
        prices = prices or {}
        names = names or {}
        self._states: dict[str, _SimState] = {}
        for sym in self.symbols:
            self._ensure(sym, names.get(sym), prices.get(sym))

    def _ensure(self, symbol: str, name: str | None = None, price: float | None = None) -> _SimState:
        if symbol not in self._states:
            dflt_name, dflt_price = DEFAULT_UNIVERSE.get(symbol, (symbol, round(self.rng.uniform(5, 60), 2)))
            self._states[symbol] = _SimState(symbol, name or dflt_name, price or dflt_price)
        return self._states[symbol]

    def add_symbol(self, symbol: str) -> None:
        super().add_symbol(symbol)
        self._ensure(symbol)

    def now(self) -> datetime:
        return self._clock

    # ---------------------------------------------------------------- clock
    def _advance(self, seconds: float) -> bool:
        """推进仿真时钟，返回是否跨入新交易日。"""
        cur = self._clock
        nxt = cur + timedelta(seconds=seconds)
        noon = datetime.combine(cur.date(), MORNING_CLOSE)
        pm_open = datetime.combine(cur.date(), AFTERNOON_OPEN)
        close = datetime.combine(cur.date(), AFTERNOON_CLOSE)
        if cur >= close:  # 收盘 -> 下一个工作日 9:25
            d = cur.date() + timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            self._clock = datetime.combine(d, time(9, 25))
            for st in self._states.values():
                st.reset_day(st.last or st.pre_close)
            return True
        if cur < noon < nxt:
            nxt = noon        # 先停在 11:30:00，让上午最后一根 K 线收盘
        elif noon <= cur < pm_open:
            nxt = pm_open     # 跳过午休
        elif cur < close < nxt:
            nxt = close       # 停在 15:00:00 收盘
        self._clock = nxt
        return False

    # ---------------------------------------------------------------- price
    def _step(self, st: _SimState, dt_seconds: float) -> Tick:
        up, down = limit_prices(st.symbol, st.pre_close, st.name)
        if st.last <= 0:  # 开盘集合竞价
            px = st.pre_close * math.exp(self.rng.gauss(0, self.daily_vol * 0.3))
            st.last = st.open = st.high = st.low = min(max(round_price(px), down), up)
        else:
            if st.drift_left <= 0:  # 随机切换趋势段，让均线类策略有信号
                st.drift = self.rng.gauss(0, self.daily_vol * 1.5) / 14400
                st.drift_left = self.rng.randint(600, 3600)
            st.drift_left -= dt_seconds
            sigma = self.daily_vol / math.sqrt(14400)
            ret = st.drift * dt_seconds + sigma * math.sqrt(dt_seconds) * self.rng.gauss(0, 1)
            st.last = min(max(round_price(st.last * math.exp(ret)), down), up)
            st.high, st.low = max(st.high, st.last), min(st.low, st.last)
        # 成交量：日内 U 型分布
        m = minutes_since_open(self._clock)
        u_shape = 1.0 + 1.5 * (math.exp(-m / 20) + math.exp(-(239 - m) / 20))
        lots = max(int(self.rng.expovariate(1 / (30 * u_shape)) * dt_seconds / 3), 0)
        vol = lots * 100
        st.volume += vol
        st.amount += vol * st.last
        return self._make_tick(st, up, down)

    def _make_tick(self, st: _SimState, up: float, down: float) -> Tick:
        def lvl() -> int:
            return self.rng.randint(1, 80) * 100

        bids = [round_price(st.last - 0.01 * i) for i in range(5)]
        asks = [round_price(st.last + 0.01 * (i + 1)) for i in range(5)]
        bid_v, ask_v = [lvl() for _ in range(5)], [lvl() for _ in range(5)]
        if st.last >= up:  # 涨停：卖盘为空，买盘堆在涨停价
            asks, ask_v = [0.0] * 5, [0] * 5
            bids = [up] + [round_price(up - 0.01 * i) for i in range(1, 5)]
            bid_v[0] *= 50
        elif st.last <= down:  # 跌停：买盘为空
            bids, bid_v = [0.0] * 5, [0] * 5
            asks = [down] + [round_price(down + 0.01 * i) for i in range(1, 5)]
            ask_v[0] *= 50
        bids = [p if p >= down else 0.0 for p in bids]
        asks = [p if p <= up else 0.0 for p in asks]
        return Tick(
            symbol=st.symbol, name=st.name, dt=self._clock, last=st.last,
            open=st.open, high=st.high, low=st.low, pre_close=st.pre_close,
            volume=st.volume, amount=round(st.amount, 2), limit_up=up, limit_down=down,
            bid_prices=bids, bid_volumes=bid_v, ask_prices=asks, ask_volumes=ask_v,
            source="sim",
        )

    def step(self) -> list[Tick]:
        """推进一步并发布 Tick（测试中可直接调用，无需事件循环）。"""
        sim_seconds = self.tick_interval * self.speed
        self._advance(sim_seconds)
        t = self._clock.time()
        if t < time(9, 25) or (MORNING_CLOSE < t < AFTERNOON_OPEN) or t > AFTERNOON_CLOSE:
            return []
        if time(9, 25) < t < MORNING_OPEN:
            return []  # 9:25~9:30 只撮合不成交，等连续竞价
        ticks = [self._step(self._ensure(s), sim_seconds) for s in list(self.symbols)]
        for tick in ticks:
            self.emit(tick)
        self.stats["polls"] += 1
        return ticks

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        self.set_state("running")
        while self._running:
            self.step()
            await self.sleep(self.tick_interval)

    def status(self) -> dict[str, Any]:
        st = super().status()
        st.update(speed=self.speed)
        return st
