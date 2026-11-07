import asyncio
from datetime import datetime, time

import pandas as pd

from quant.core.events import TICK, EventBus
from quant.data.akshare_feed import AkshareFeed, Push2RateLimitError, parse_bid_ask, parse_spot_row
from quant.data.history import akshare_daily_to_bars, akshare_minute_to_bars, make_synthetic_daily
from quant.data.sim_feed import SimFeed
from quant.data.store import Store

BID_ASK = {
    "sell_5": 10.55, "sell_5_vol": 1200, "sell_4": 10.54, "sell_4_vol": 300, "sell_3": 10.53, "sell_3_vol": 100,
    "sell_2": 10.52, "sell_2_vol": 800, "sell_1": 10.51, "sell_1_vol": 500,
    "buy_1": 10.50, "buy_1_vol": 2000, "buy_2": 10.49, "buy_2_vol": 100, "buy_3": 10.48, "buy_3_vol": 100,
    "buy_4": "-", "buy_4_vol": "-" * 100, "buy_5": "-", "buy_5_vol": "-" * 100,
    "最新": 10.50, "均价": 10.47, "涨幅": 0.96, "涨跌": 0.1, "总手": 123456, "金额": 1.29e8, "换手": 0.04,
    "量比": 1.1, "最高": 10.6, "最低": 10.3, "今开": 10.4, "昨收": 10.4, "涨停": 11.44, "跌停": 9.36,
}


def test_parse_bid_ask_handles_dashes():
    t = parse_bid_ask("600000", BID_ASK, datetime(2026, 11, 9, 10, 0))
    assert t.last == 10.5 and t.volume == 12_345_600 and t.limit_up == 11.44
    assert t.ask_prices[0] == 10.51 and t.bid_prices[3] == 0.0 and t.bid_volumes[4] == 0
    assert parse_bid_ask("600000", {**BID_ASK, "最新": "-"}, datetime.now()) is None


def test_parse_spot_row():
    row = {"代码": "1", "名称": "平安银行", "最新价": 11.2, "成交量": 1000, "成交额": 1.1e6,
           "最高": 11.3, "最低": 11.0, "今开": 11.1, "昨收": 11.0}
    t = parse_spot_row(row, datetime.now())
    assert t.symbol == "000001" and t.volume == 100_000 and t.limit_up == 12.1 and t.bid_prices == []


def test_akshare_history_conversion():
    daily = pd.DataFrame({"日期": ["2026-11-06"], "股票代码": ["600000"], "开盘": [10.0], "收盘": [10.5], "最高": [10.6],
                          "最低": [9.9], "成交量": [1000], "成交额": [1.0e6], "振幅": [7], "涨跌幅": [5], "涨跌额": [0.5], "换手率": [1]})
    b = akshare_daily_to_bars(daily, "600000")
    assert b.iloc[0]["pre_close"] == 10.0 and b.iloc[0]["volume"] == 100_000
    minute = pd.DataFrame({"时间": ["2026-11-06 09:30:00", "2026-11-06 09:31:00", "2026-11-06 09:32:00"],
                           "开盘": [10, 10.1, 10.2], "收盘": [10, 10.2, 10.3], "最高": [10, 10.3, 10.4], "最低": [10, 10, 10.1],
                           "成交量": [50, 100, 120], "成交额": [5e4, 1e5, 1.2e5]})
    m = akshare_minute_to_bars(minute, "600000")
    # 09:30 集合竞价线并入 9:30，09:31（结束时间）-> 9:30 起始
    assert list(m["dt"].dt.strftime("%H:%M")) == ["09:30", "09:31"]
    assert m.iloc[0]["volume"] == 15_000 and m.iloc[0]["open"] == 10 and m.iloc[0]["close"] == 10.2


def test_store_roundtrip():
    s = Store(":memory:")
    s.write_bars_df(make_synthetic_daily("600000", "2025-01-01", "2025-03-31", seed=1))
    s.write_bars_df(make_synthetic_daily("600000", "2025-03-01", "2025-03-31", seed=2))  # 覆盖不重复
    df = s.load_bars("600000", "1d")
    assert len(df) == len(pd.bdate_range("2025-01-01", "2025-03-31"))
    assert len(s.load_bars("600000", "1d", limit=5)) == 5


def test_sim_feed_rules():
    bus = EventBus()
    ticks = []
    bus.on(TICK, ticks.append)
    feed = SimFeed(bus, ["600000", "300750"], speed=600, tick_interval=1, seed=3,
                   start=datetime(2026, 11, 9, 9, 25))
    for _ in range(300):  # 300 步 * 600 秒，跨越多个交易日
        feed.step()
    assert ticks
    for t in ticks:
        assert t.limit_down <= t.last <= t.limit_up
        tt = t.dt.time()
        # 9:25:00 为开盘集合竞价快照，其余必须在连续竞价时段内
        assert tt == time(9, 25) or time(9, 30) <= tt <= time(11, 30) or time(13, 0) <= tt <= time(15, 0)
        assert t.dt.weekday() < 5
    days = sorted({t.dt.date() for t in ticks})
    assert len(days) >= 2
    first_day2 = next(t for t in ticks if t.dt.date() == days[1] and t.symbol == "600000")
    last_day1 = [t for t in ticks if t.dt.date() == days[0] and t.symbol == "600000"][-1]
    assert first_day2.pre_close == last_day1.last


def test_akshare_feed_dedup_and_rate_limit(monkeypatch):
    bus = EventBus()
    got = []
    bus.on(TICK, got.append)
    feed = AkshareFeed(bus, ["600000", "000001"], interval=1, only_trading_hours=False, ban_backoff=60)
    calls = {"n": 0}

    def fake(symbol):
        calls["n"] += 1
        return dict(BID_ASK)

    monkeypatch.setattr(feed, "_fetch_quote", fake)
    assert asyncio.run(feed.poll_once()) == 2
    assert asyncio.run(feed.poll_once()) == 0   # 行情没变化，不重复发布
    assert len(got) == 2

    def banned(symbol):
        raise Push2RateLimitError("ban")

    monkeypatch.setattr(feed, "_fetch_quote", banned)

    async def run_briefly():
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.3)
        feed.stop()
        await task

    asyncio.run(run_briefly())
    assert feed.state == "idle" and feed._ban_until is not None and feed._ban_backoff == 120
