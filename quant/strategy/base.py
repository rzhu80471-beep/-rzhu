"""策略基类。同一份策略代码可以跑实时模拟盘、实盘（QMT）和日线回测。

写策略只需继承 Strategy，重写 on_bar / on_tick，用 buy / sell / buy_value / sell_all 下单。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from ..core.models import AccountSnapshot, Bar, Direction, Order, OrderType, Position, Tick, Trade
from ..core.rules import round_buy_volume


class StrategyContext(Protocol):
    def send_order(self, strategy: "Strategy", symbol: str, direction: Direction, volume: int,
                   price: float | None = None, order_type: OrderType = OrderType.LIMIT) -> Order | None: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def get_position(self, symbol: str) -> Position: ...
    def get_account(self) -> AccountSnapshot: ...
    def get_last_price(self, symbol: str) -> float: ...
    def get_tick(self, symbol: str) -> Tick | None: ...
    def now(self) -> datetime: ...
    def write_log(self, message: str, source: str, level: str = "info") -> None: ...


class Strategy:
    #: 策略接收的 K 线周期："1m" / "5m" / "15m" / "30m" / "60m"（实时）或 "1d"（回测）
    interval: str = "1m"
    #: 预热需要的 K 线数量
    warmup: int = 0
    default_params: dict[str, Any] = {}

    def __init__(self, ctx: StrategyContext, symbols: list[str], name: str | None = None, **params: Any):
        self.ctx = ctx
        self.symbols = list(symbols)
        self.name = name or type(self).__name__
        self.params = {**self.default_params, **params}
        for k, v in self.params.items():
            setattr(self, k, v)
        self.trading = False   # 预热阶段为 False，下单会被忽略
        self.errors = 0
        self._log = logging.getLogger(f"strategy.{self.name}")

    # ---------------------------------------------------------------- 回调
    def on_init(self) -> None: ...
    def on_start(self) -> None: ...
    def on_stop(self) -> None: ...
    def on_tick(self, tick: Tick) -> None: ...
    def on_bar(self, bar: Bar) -> None: ...
    def on_order(self, order: Order) -> None: ...
    def on_trade(self, trade: Trade) -> None: ...

    # ---------------------------------------------------------------- 交易
    def buy(self, symbol: str, volume: int, price: float | None = None) -> Order | None:
        return self._send(symbol, Direction.BUY, volume, price)

    def sell(self, symbol: str, volume: int, price: float | None = None) -> Order | None:
        return self._send(symbol, Direction.SELL, volume, price)

    def buy_value(self, symbol: str, value: float, price: float | None = None) -> Order | None:
        """按金额买入，自动换算成合法股数（扣除约 0.1% 费用余量）。"""
        ref = price or self.ctx.get_last_price(symbol)
        if ref <= 0:
            return None
        volume = round_buy_volume(symbol, value / (ref * 1.001))
        return self.buy(symbol, volume, price) if volume > 0 else None

    def sell_all(self, symbol: str, price: float | None = None) -> Order | None:
        avail = self.ctx.get_position(symbol).available
        return self.sell(symbol, avail, price) if avail > 0 else None

    def cancel(self, order_id: str) -> bool:
        return self.ctx.cancel_order(order_id)

    def _send(self, symbol: str, direction: Direction, volume: int, price: float | None) -> Order | None:
        if not self.trading or volume <= 0:
            return None
        otype = OrderType.MARKET if price is None else OrderType.LIMIT
        return self.ctx.send_order(self, symbol, direction, volume, price, otype)

    # ---------------------------------------------------------------- 查询
    def pos(self, symbol: str) -> int:
        return self.ctx.get_position(symbol).volume

    def available(self, symbol: str) -> int:
        return self.ctx.get_position(symbol).available

    @property
    def equity(self) -> float:
        return self.ctx.get_account().equity

    @property
    def cash(self) -> float:
        return self.ctx.get_account().cash

    def log(self, message: str, level: str = "info") -> None:
        self.ctx.write_log(message, self.name, level)

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "class": type(self).__name__, "symbols": self.symbols,
                "interval": self.interval, "params": self.params, "trading": self.trading, "errors": self.errors}
