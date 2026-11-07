"""示例策略 2：唐奇安通道突破 + ATR 止损（海龟思路的简化版）。

收盘价突破前 N 根最高价买入；跌破前 M 根最低价、或从买入价回撤 k 倍 ATR 时卖出。
"""
from __future__ import annotations

from ...core.models import Bar, Direction, Trade
from ..base import Strategy
from ..series import BarSeries


class BreakoutStrategy(Strategy):
    interval = "1d"
    default_params = {"entry": 20, "exit": 10, "atr_n": 14, "atr_stop": 2.0, "position_pct": 0.25}

    def on_init(self) -> None:
        size = max(self.entry, self.exit, self.atr_n) + 2
        self.warmup = size
        self.series = {s: BarSeries(size) for s in self.symbols}
        self.entry_price: dict[str, float] = {}

    def on_trade(self, trade: Trade) -> None:
        if trade.direction == Direction.BUY:
            self.entry_price[trade.symbol] = trade.price

    def on_bar(self, bar: Bar) -> None:
        s = self.series.get(bar.symbol)
        if s is None:
            return
        s.update(bar)
        if not s.ready:
            return
        sym = bar.symbol
        if self.pos(sym) == 0:
            if bar.close > s.highest(self.entry):
                self.buy_value(sym, self.equity * self.position_pct)
        elif self.available(sym) > 0:
            stop = self.entry_price.get(sym, bar.close) - self.atr_stop * s.atr(self.atr_n)
            if bar.close < s.lowest(self.exit) or bar.close < stop:
                self.sell_all(sym)
