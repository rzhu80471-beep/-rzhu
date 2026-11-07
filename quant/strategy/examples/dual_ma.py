"""示例策略 1：双均线趋势跟踪。快线上穿慢线买入固定比例仓位，下穿卖出全部可卖持仓。"""
from __future__ import annotations

from ...core.models import Bar
from ..base import Strategy
from ..series import BarSeries


class DualMaStrategy(Strategy):
    interval = "1m"
    default_params = {"fast": 5, "slow": 20, "position_pct": 0.2}

    def on_init(self) -> None:
        self.warmup = self.slow + 2
        self.series = {s: BarSeries(self.slow + 2) for s in self.symbols}

    def on_bar(self, bar: Bar) -> None:
        s = self.series.get(bar.symbol)
        if s is None:
            return
        s.update(bar)
        if not s.ready:
            return
        fast, slow = s.sma(self.fast, array=True), s.sma(self.slow, array=True)
        cross_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        cross_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        if cross_up and self.pos(bar.symbol) == 0:
            order = self.buy_value(bar.symbol, self.equity * self.position_pct)
            if order:
                self.log(f"{bar.symbol} 金叉买入 {order.volume} 股 @ {bar.close}")
        elif cross_down and self.available(bar.symbol) > 0:
            order = self.sell_all(bar.symbol)
            if order:
                self.log(f"{bar.symbol} 死叉卖出 {order.volume} 股 @ {bar.close}")
