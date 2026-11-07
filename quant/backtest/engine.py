"""日线事件驱动回测，与实时引擎共用 Strategy 和 PaperBroker。

时序（避免未来函数）：
  第 t 日收盘 -> strategy.on_bar(bar_t) 产生委托（只进入待报队列）
  第 t+1 日开盘 -> 换日（T+1 解锁昨日买入）-> 待报委托经风控后报给撮合 -> 用 bar_{t+1} 撮合
  -> 当日未成交的委托撤销 -> strategy.on_bar(bar_{t+1}) ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from ..core.models import AccountSnapshot, Bar, Direction, Order, OrderStatus, OrderType, Position, Tick, Trade
from ..core.rules import FeeModel
from ..engine.broker import PaperBroker
from ..engine.risk import RiskConfig, RiskManager
from ..strategy import Strategy
from .metrics import performance


@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    equity: pd.DataFrame
    trades: list[Trade]
    orders: list[Order]
    logs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        m = self.metrics
        if not m:
            return "无回测结果（数据不足）"
        pct = lambda x: f"{x * 100:.2f}%"  # noqa: E731
        lines = [
            f"区间        {m['start']} ~ {m['end']}（{m['days']} 个交易日）",
            f"期末权益    {m['final_equity']:,.2f}",
            f"总收益      {pct(m['total_return'])}    基准 {pct(m.get('benchmark_return', 0))}",
            f"年化收益    {pct(m['annual_return'])}",
            f"最大回撤    {pct(m['max_drawdown'])}",
            f"夏普 / 索提诺 / 卡玛  {m['sharpe']:.2f} / {m['sortino']:.2f} / {m['calmar']:.2f}",
            f"成交笔数    {m['trades']}    平仓 {m['round_trips']} 次，胜率 {pct(m['win_rate'])}，盈亏比 {m['profit_factor']:.2f}",
            f"总费用      {m['total_fees']:,.2f}    被拒/未成交委托 {m.get('rejected', 0)} / {m.get('unfilled', 0)}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        eq = self.equity.reset_index()
        eq["date"] = eq["date"].dt.strftime("%Y-%m-%d")
        return {
            "metrics": self.metrics,
            "equity": eq.to_dict("records"),
            "trades": [t.to_dict() for t in self.trades],
            "logs": self.logs[-200:],
        }


def df_to_bars(df: pd.DataFrame) -> list[Bar]:
    return [Bar(r.symbol, pd.Timestamp(r.dt).to_pydatetime(), r.interval, r.open, r.high, r.low, r.close,
                int(r.volume), r.amount, r.pre_close) for r in df.itertuples()]


class BacktestEngine:
    def __init__(self, data: dict[str, pd.DataFrame], initial_cash: float = 1_000_000, fees: FeeModel | None = None,
                 risk: RiskConfig | None = None):
        self.data = {s: df.sort_values("dt").reset_index(drop=True) for s, df in data.items() if not df.empty}
        self.broker = PaperBroker(initial_cash, fees or FeeModel(), expire_on_new_day=False)
        # 回测默认只保留仓位占比约束；单笔金额 / 报单频率这类实盘约束不限制
        rc = risk or RiskConfig(max_order_value=float("inf"), max_position_pct=1.0)
        rc.max_orders_per_second = None
        self.risk = RiskManager(rc)
        self._pending: list[Order] = []
        self._risk_rejected: list[Order] = []
        self._last: dict[str, float] = {}
        self._now: datetime | None = None
        self.logs: list[str] = []
        self.rejected = 0
        self.unfilled = 0
        self.strategy: Strategy | None = None

    # ---------------------------------------------------------------- StrategyContext
    def send_order(self, strategy: Strategy, symbol: str, direction: Direction, volume: int,
                   price: float | None = None, order_type: OrderType = OrderType.LIMIT) -> Order:
        order = Order(symbol, direction, float(price or 0.0), int(volume), order_type, strategy=strategy.name)
        order.created = self._now
        self._pending.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        before = len(self._pending)
        self._pending = [o for o in self._pending if o.id != order_id]
        return len(self._pending) < before or self.broker.cancel(order_id)

    def get_position(self, symbol: str) -> Position:
        return self.broker.get_position(symbol)

    def get_account(self) -> AccountSnapshot:
        return self.broker.account()

    def get_last_price(self, symbol: str) -> float:
        return self._last.get(symbol, 0.0)

    def get_tick(self, symbol: str) -> Tick | None:
        return None

    def now(self) -> datetime:
        return self._now or datetime.min

    def write_log(self, message: str, source: str, level: str = "info") -> None:
        self.logs.append(f"{self._now:%Y-%m-%d} [{source}] {message}")

    # ---------------------------------------------------------------- run
    def _on_order(self, order: Order) -> None:
        if order.status == OrderStatus.REJECTED:
            self.rejected += 1
            self.write_log(f"拒单 {order.symbol} {order.direction.value} {order.volume}: {order.reason}", order.strategy)
        if self.strategy:
            self.strategy.on_order(order)

    def run(self, strategy_cls: type[Strategy], symbols: list[str] | None = None,
            params: dict[str, Any] | None = None, name: str | None = None,
            start: str | None = None, end: str | None = None) -> BacktestResult:
        symbols = [s for s in (symbols or list(self.data)) if s in self.data]
        if not symbols:
            raise ValueError("没有可用于回测的数据")
        strat = strategy_cls(self, symbols, name=name, **(params or {}))
        self.strategy = strat
        strat.on_init()
        self.broker.on_order = self._on_order
        self.broker.on_trade = strat.on_trade

        frames = []
        for s in symbols:
            df = self.data[s]
            if start:
                df = df[df["dt"] >= pd.Timestamp(start)]
            if end:
                df = df[df["dt"] <= pd.Timestamp(end)]
            frames.append(df)
        allbars = pd.concat(frames).sort_values(["dt", "symbol"])
        by_day = {d: df_to_bars(g) for d, g in allbars.groupby("dt", sort=True)}

        strat.trading = True
        strat.on_start()
        curve = []
        first_close: dict[str, float] = {}
        last_close: dict[str, float] = {}
        for day, bars in by_day.items():
            self._now = pd.Timestamp(day).to_pydatetime()
            self.broker.set_time(self._now)
            today = {b.symbol: b for b in bars}
            for b in bars:
                self.broker.set_limits(b.symbol, b.pre_close or self._last.get(b.symbol, b.open))
            # 1) 昨日收盘后产生的委托，今日开盘报单
            pending, self._pending = self._pending, []
            for order in pending:
                if order.symbol not in today:
                    order.status, order.reason = OrderStatus.REJECTED, "停牌，无法交易"
                    self._on_order(order)
                    continue
                pos_val = self.broker.get_position(order.symbol).volume * today[order.symbol].open
                ref = today[order.symbol].open
                if reason := self.risk.check(order, self.broker.account(), pos_val, ref_price=ref):
                    order.status, order.reason = OrderStatus.REJECTED, f"[风控] {reason}"
                    self._risk_rejected.append(order)
                    self._on_order(order)
                    continue
                self.broker.submit(order)
            # 2) 撮合
            for b in bars:
                self.broker.match_bar(b)
            # 3) 当日有效：未成交撤单
            self.unfilled += self.broker.cancel_all("当日未成交")
            # 4) 收盘，策略计算
            for b in bars:
                self._last[b.symbol] = b.close
                first_close.setdefault(b.symbol, b.close)
                last_close[b.symbol] = b.close
                strat.on_bar(b)
            acct = self.broker.account()
            bench = sum(last_close[s] / first_close[s] for s in first_close) / len(first_close)
            curve.append((self._now, acct.equity, acct.cash, acct.market_value, bench))
        strat.on_stop()

        eq = pd.DataFrame(curve, columns=["date", "equity", "cash", "market_value", "benchmark"]).set_index("date")
        eq.index = pd.to_datetime(eq.index)
        eq["benchmark"] = eq["benchmark"] * self.broker.initial_cash
        eq["drawdown"] = eq["equity"] / eq["equity"].cummax() - 1
        trades = self.broker.trades()
        metrics = performance(eq["equity"], trades)
        if metrics:
            metrics["benchmark_return"] = float(eq["benchmark"].iloc[-1] / eq["benchmark"].iloc[0] - 1)
            metrics["rejected"] = self.rejected
            metrics["unfilled"] = self.unfilled
        return BacktestResult(metrics, eq, trades, self.broker.orders() + self._risk_rejected, self.logs)
