"""实时交易引擎：行情源 -> 事件总线 -> [撮合 -> K 线合成 -> 策略] -> 风控 -> 券商 -> 落盘 / 推送。

    DataFeed ──Tick──▶ EventBus ──▶ Broker.on_tick（先撮合挂单）
                                 ├─▶ BarGenerator ──Bar(1m)──▶ WindowBarGenerator(5m/15m/…)
                                 │                              └──▶ Strategy.on_bar
                                 ├─▶ Strategy.on_tick
                                 └─▶ StoreWriter / WebSocket（异步队列，慢消费者不阻塞行情）
    Strategy.buy/sell ──▶ RiskManager.check ──▶ Broker.submit ──Order/Trade──▶ EventBus
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from ..core.events import ACCOUNT, BAR, LOG, ORDER, POSITION, TICK, TRADE, EventBus
from ..core.models import AccountSnapshot, Bar, Direction, Order, OrderStatus, OrderType, Position, Tick, Trade
from ..core.rules import FeeModel
from ..data.bar_generator import BarGenerator, WindowBarGenerator
from ..data.base import DataFeed
from ..data.store import Store, StoreWriter
from ..strategy import Strategy, load_strategy_class
from .broker import Broker, PaperBroker
from .risk import RiskConfig, RiskManager

log = logging.getLogger(__name__)

MAX_STRATEGY_ERRORS = 5


def build_feed(cfg: dict[str, Any], bus: EventBus) -> DataFeed:
    fc = cfg["feed"]
    symbols, names = fc["symbols"], fc.get("names") or {}
    kind = fc["type"]
    if kind == "sim":
        from ..data.sim_feed import SimFeed

        return SimFeed(bus, symbols, names=names, **fc.get("sim", {}))
    if kind == "akshare":
        from ..data.akshare_feed import AkshareFeed

        return AkshareFeed(bus, symbols, names=names, **fc.get("akshare", {}))
    if kind == "qmt":
        from .qmt import QmtFeed

        return QmtFeed(bus, symbols, **fc.get("qmt", {}))
    raise ValueError(f"未知行情源类型: {kind}")


def build_broker(cfg: dict[str, Any]) -> Broker:
    bc = cfg["broker"]
    if bc["type"] == "paper":
        return PaperBroker(bc["initial_cash"], FeeModel(**bc.get("fees", {})), fill_ratio=bc.get("fill_ratio", 1.0))
    if bc["type"] == "qmt":
        from .qmt import QmtBroker

        return QmtBroker(**bc["qmt"])
    raise ValueError(f"未知券商类型: {bc['type']}")


class LiveEngine:
    def __init__(self, cfg: dict[str, Any], bus: EventBus | None = None, feed: DataFeed | None = None,
                 broker: Broker | None = None, store: Store | None = None):
        self.cfg = cfg
        self.bus = bus or EventBus()
        self.feed = feed or build_feed(cfg, self.bus)
        self.broker = broker or build_broker(cfg)
        self.store = store if store is not None else Store(cfg["storage"]["path"])
        self.writer = StoreWriter(self.bus, self.store, cfg["storage"].get("store_ticks", True))
        self.risk = RiskManager(RiskConfig(**(cfg.get("risk") or {})))
        self.names: dict[str, str] = dict(cfg["feed"].get("names") or {})
        self.ticks: dict[str, Tick] = {}
        self.bars: dict[tuple[str, str], deque[Bar]] = defaultdict(lambda: deque(maxlen=2000))
        self.logs: deque[dict] = deque(maxlen=500)
        self.strategies: list[Strategy] = []
        self._windows: dict[int, WindowBarGenerator] = {}
        self._tasks: list[asyncio.Task] = []
        self.started_at: datetime | None = None

        self.bar_gen = BarGenerator(lambda bar: self.bus.publish(BAR, bar))
        self.broker.on_order = lambda o: self.bus.publish(ORDER, o)
        self.broker.on_trade = lambda t: self.bus.publish(TRADE, t)
        self.bus.on(TICK, self._on_tick)
        self.bus.on(BAR, self._on_bar)
        self.bus.on(ORDER, self._on_order)
        self.bus.on(TRADE, self._on_trade)
        self.bus.on(LOG, self.logs.append)

        for scfg in cfg.get("strategies") or []:
            self.add_strategy(scfg)

    # ---------------------------------------------------------------- strategies
    def add_strategy(self, scfg: dict[str, Any]) -> Strategy:
        cls = load_strategy_class(scfg["class"])
        strat = cls(self, scfg.get("symbols") or self.feed.symbols, name=scfg.get("name"), **(scfg.get("params") or {}))
        if scfg.get("interval"):
            strat.interval = scfg["interval"]
        if strat.interval.endswith("m") and strat.interval != "1m":
            n = int(strat.interval[:-1])
            if n not in self._windows:
                self._windows[n] = WindowBarGenerator(n, lambda bar: self.bus.publish(BAR, bar))
        elif strat.interval not in ("1m",):
            raise ValueError(f"实时引擎不支持周期 {strat.interval}（日线策略请用回测引擎）")
        for sym in strat.symbols:
            self.feed.add_symbol(sym)
        self._call(strat, "on_init")
        self.strategies.append(strat)
        return strat

    def _call(self, strat: Strategy, method: str, *args: Any) -> None:
        if strat.errors >= MAX_STRATEGY_ERRORS:
            return
        try:
            getattr(strat, method)(*args)
        except Exception as exc:  # noqa: BLE001
            strat.errors += 1
            log.exception("策略 %s.%s 出错", strat.name, method)
            self.write_log(f"{method} 出错: {exc!r}", strat.name, "error")
            if strat.errors >= MAX_STRATEGY_ERRORS:
                strat.trading = False
                self.write_log(f"连续出错 {MAX_STRATEGY_ERRORS} 次，策略已停止", strat.name, "error")

    def _warmup(self, strat: Strategy) -> None:
        """用数据库里的历史 1 分钟 K 线预热（预热期间 trading=False，不会下单）。"""
        window = int(strat.interval[:-1])
        n = max(strat.warmup, 0) * window
        if n <= 0:
            return
        gen = WindowBarGenerator(window, lambda b: self._call(strat, "on_bar", b)) if window > 1 else None
        for sym in strat.symbols:
            df = self.store.load_bars(sym, "1m", limit=n)
            for row in df.itertuples():
                bar = Bar(sym, row.dt.to_pydatetime(), "1m", row.open, row.high, row.low, row.close, int(row.volume), row.amount)
                if gen:
                    gen.update_bar(bar)
                else:
                    self._call(strat, "on_bar", bar)

    # ---------------------------------------------------------------- event handlers
    def _on_tick(self, tick: Tick) -> None:
        if tick.name:
            self.names.setdefault(tick.symbol, tick.name)
        elif tick.symbol in self.names:
            tick.name = self.names[tick.symbol]
        self.ticks[tick.symbol] = tick
        self.broker.on_tick(tick)
        self.bar_gen.update_tick(tick)
        for strat in self.strategies:
            if tick.symbol in strat.symbols:
                self._call(strat, "on_tick", tick)

    def _on_bar(self, bar: Bar) -> None:
        self.bars[(bar.symbol, bar.interval)].append(bar)
        if bar.interval == "1m":
            for gen in self._windows.values():
                gen.update_bar(bar)
        for strat in self.strategies:
            if strat.interval == bar.interval and bar.symbol in strat.symbols:
                self._call(strat, "on_bar", bar)

    def _strategy_of(self, name: str) -> Strategy | None:
        return next((s for s in self.strategies if s.name == name), None)

    def _on_order(self, order: Order) -> None:
        if order.status == OrderStatus.REJECTED:
            self.write_log(f"委托被拒 {order.symbol} {order.direction.value} {order.volume}@{order.price}: {order.reason}", order.strategy, "warning")
        strat = self._strategy_of(order.strategy)
        if strat:
            self._call(strat, "on_order", order)

    def _on_trade(self, trade: Trade) -> None:
        self.write_log(f"成交 {trade.symbol} {'买入' if trade.direction == Direction.BUY else '卖出'} {trade.volume}@{trade.price:.2f}", trade.strategy)
        strat = self._strategy_of(trade.strategy)
        if strat:
            self._call(strat, "on_trade", trade)

    # ---------------------------------------------------------------- StrategyContext
    def send_order(self, strategy: Strategy | None, symbol: str, direction: Direction, volume: int,
                   price: float | None = None, order_type: OrderType = OrderType.LIMIT) -> Order:
        name = strategy.name if strategy else "manual"
        order = Order(symbol, direction, float(price or 0.0), int(volume), order_type, strategy=name)
        tick = self.ticks.get(symbol)
        ref = tick.last if tick else 0.0
        if order_type == OrderType.MARKET and tick:
            ref = tick.limit_up if direction == Direction.BUY and tick.limit_up else ref
        reason = self.risk.check(order, self.broker.account(), self.broker.get_position(symbol).market_value,
                                 self.names.get(symbol, ""), ref)
        if reason:
            self.risk.rejected += 1
            order.status = OrderStatus.REJECTED
            order.reason = f"[风控] {reason}"
            order.created = order.updated = self.now()
            self.bus.publish(ORDER, order)
            return order
        return self.broker.submit(order)

    def cancel_order(self, order_id: str) -> bool:
        if reason := self.risk.check_cancel():
            self.write_log(reason, "risk", "warning")
            return False
        return self.broker.cancel(order_id)

    def get_position(self, symbol: str) -> Position:
        return self.broker.get_position(symbol)

    def get_account(self) -> AccountSnapshot:
        return self.broker.account()

    def get_last_price(self, symbol: str) -> float:
        t = self.ticks.get(symbol)
        return t.last if t else 0.0

    def get_tick(self, symbol: str) -> Tick | None:
        return self.ticks.get(symbol)

    def now(self) -> datetime:
        return self.feed.now()

    def write_log(self, message: str, source: str = "engine", level: str = "info") -> None:
        self.bus.log(message, level, source)

    # ---------------------------------------------------------------- lifecycle
    async def _timer(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.bar_gen.flush(self.feed.now())
                acct = self.broker.account()
                if reason := self.risk.on_account(acct):
                    self.write_log(f"触发风控熔断：{reason}", "risk", "error")
                self.bus.publish(ACCOUNT, acct)
                self.bus.publish(POSITION, self.broker.positions())
            except Exception:  # noqa: BLE001
                log.exception("定时任务出错")

    async def start(self) -> None:
        self.bus.bind_loop()
        self.started_at = datetime.now()
        for strat in self.strategies:
            self._warmup(strat)
            strat.trading = True
            self._call(strat, "on_start")
        if hasattr(self.broker, "start"):
            await self.broker.start(self.bus)  # type: ignore[attr-defined]
        self._tasks = [
            asyncio.create_task(self.feed.run(), name="feed"),
            asyncio.create_task(self.writer.run(), name="store-writer"),
            asyncio.create_task(self._timer(), name="timer"),
        ]
        self.write_log(f"引擎启动：行情源={self.feed.source}，券商={self.broker.name}，策略={[s.name for s in self.strategies]}")

    async def stop(self) -> None:
        for strat in self.strategies:
            self._call(strat, "on_stop")
            strat.trading = False
        self.feed.stop()
        self.bar_gen.flush_all()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.writer.flush()
        self.writer.stop()
        if hasattr(self.broker, "stop"):
            self.broker.stop()  # type: ignore[attr-defined]

    def status(self) -> dict[str, Any]:
        return {
            "feed": self.feed.status(),
            "broker": self.broker.name,
            "risk": self.risk.status(),
            "strategies": [s.info() for s in self.strategies],
            "events": dict(self.bus.counters),
            "stored": self.writer.written,
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
        }
