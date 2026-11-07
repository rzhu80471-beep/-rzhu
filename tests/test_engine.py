import asyncio
from datetime import datetime

import pandas as pd
import pytest

from quant.backtest.engine import BacktestEngine
from quant.config import load_config
from quant.core.models import Bar, Direction, OrderStatus, OrderType
from quant.data.history import make_synthetic_daily
from quant.data.sim_feed import SimFeed
from quant.data.store import Store
from quant.engine.live import LiveEngine
from quant.strategy import Strategy, load_strategy_class


def make_engine(strategies=None, risk=None):
    cfg = load_config(None)
    cfg["storage"]["path"] = ":memory:"
    cfg["strategies"] = strategies or []
    cfg["risk"] = risk or {}
    eng = LiveEngine(cfg, store=Store(":memory:"))
    eng.feed = SimFeed(eng.bus, ["600000", "000001"], speed=60, tick_interval=1, seed=7,
                       start=datetime(2026, 11, 9, 9, 29, 0))
    return eng


def test_pipeline_tick_to_bar_to_strategy():
    eng = make_engine([{"class": "dual_ma", "symbols": ["600000"], "params": {"fast": 2, "slow": 4}}])
    strat = eng.strategies[0]
    strat.trading = True
    seen = []
    orig = strat.on_bar
    strat.on_bar = lambda b: (seen.append(b), orig(b))
    for _ in range(40):
        eng.feed.step()
        eng.bar_gen.flush(eng.feed.now())
    assert len(seen) >= 30
    assert all(b.symbol == "600000" and b.interval == "1m" for b in seen)
    assert len(eng.bars[("000001", "1m")]) >= 30


def test_risk_rejects_and_kill_switch():
    eng = make_engine(risk={"max_order_value": 5000, "max_orders_per_second": None})
    eng.feed.step()
    eng.feed.step()
    o = eng.send_order(None, "600000", Direction.BUY, 1000, 10.6)
    assert o.status == OrderStatus.REJECTED and "单笔金额" in o.reason
    o = eng.send_order(None, "600000", Direction.BUY, 100, None, OrderType.MARKET)
    assert o.status == OrderStatus.FILLED
    eng.risk.set_kill_switch(True, "测试")
    o = eng.send_order(None, "600000", Direction.BUY, 100, None, OrderType.MARKET)
    assert o.status == OrderStatus.REJECTED and "熔断" in o.reason


def test_strategy_errors_are_isolated():
    class Boom(Strategy):
        def on_bar(self, bar):
            raise RuntimeError("bug")

    eng = make_engine()
    s = Boom(eng, ["600000"])
    eng.strategies.append(s)
    s.trading = True
    for _ in range(20):
        eng.feed.step()
    assert s.errors == 5 and s.trading is False   # 连续出错后自动停用，引擎照常运行
    assert eng.ticks["600000"].last > 0


def test_live_engine_async_start_stop():
    eng = make_engine([{"class": "dual_ma", "symbols": ["600000"]}])
    eng.feed.tick_interval = 0.02

    async def go():
        await eng.start()
        await asyncio.sleep(1.5)
        await eng.stop()

    asyncio.run(go())
    assert eng.feed.stats["ticks"] > 20
    assert eng.writer.written["ticks"] > 0


class BuyFirstSellNext(Strategy):
    """第 1 根 K 线买入，之后每根都尝试卖出 —— 用来检验无未来函数和 T+1。"""

    def on_bar(self, bar):
        if self.pos(bar.symbol) == 0 and not getattr(self, "done", False):
            self.buy(bar.symbol, 1000)
            self.done = True
        elif self.available(bar.symbol) > 0:
            self.sell_all(bar.symbol)


def test_backtest_next_open_and_t_plus_one():
    dates = pd.bdate_range("2025-01-06", periods=4)
    df = pd.DataFrame({"symbol": "600000", "interval": "1d", "dt": dates,
                       "open": [10.0, 10.2, 10.4, 10.6], "high": [10.5, 10.6, 10.8, 11.0],
                       "low": [9.9, 10.0, 10.3, 10.5], "close": [10.1, 10.3, 10.5, 10.9],
                       "volume": 1_000_000, "amount": 1e7, "pre_close": [10.0, 10.1, 10.3, 10.5]})
    res = BacktestEngine({"600000": df}).run(BuyFirstSellNext)
    buy, sell = res.trades
    assert buy.direction == Direction.BUY and buy.dt == dates[1] and buy.price == 10.2   # 次日开盘成交
    assert sell.direction == Direction.SELL and sell.dt == dates[3] and sell.price == 10.6  # T+1 后再次日开盘
    assert res.metrics["trades"] == 2


@pytest.mark.parametrize("name", ["dual_ma", "breakout"])
def test_builtin_strategies_backtest(name):
    data = {s: make_synthetic_daily(s, "2023-01-01", "2024-12-31", seed=i) for i, s in enumerate(["600000", "300750"])}
    res = BacktestEngine(data).run(load_strategy_class(name))
    m = res.metrics
    assert m["days"] > 400 and m["trades"] > 0
    assert -1 < m["max_drawdown"] <= 0
    assert res.equity["equity"].iloc[0] == pytest.approx(1_000_000, rel=0.01)
    for t in res.trades:  # 所有成交都遵守整手
        assert t.volume % 100 == 0
