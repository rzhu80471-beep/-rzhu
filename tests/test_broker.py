from datetime import datetime

import pytest

from quant.core.models import Bar, Direction, Order, OrderStatus, OrderType
from quant.engine.broker import PaperBroker

DAY1 = datetime(2026, 11, 9, 10, 0, 0)
DAY2 = datetime(2026, 11, 10, 10, 0, 0)


@pytest.fixture
def broker():
    b = PaperBroker(100_000)
    b.orders_seen, b.trades_seen = [], []
    b.on_order = b.orders_seen.append
    b.on_trade = b.trades_seen.append
    return b


def test_buy_walks_the_book_and_freezes_cash(broker, tick_factory):
    broker.on_tick(tick_factory(dt=DAY1, last=10.0, asks=[10.01, 10.02, 10.03], ask_vols=[300, 300, 5000]))
    o = broker.submit(Order("600000", Direction.BUY, 10.02, 1000))
    # 卖一 300 + 卖二 300 成交，剩余 400 挂单
    assert o.status == OrderStatus.PARTIAL and o.traded == 600
    assert [t.price for t in broker.trades_seen] == [10.01, 10.02]
    assert o.frozen_cash > 0
    assert broker.cancel(o.id)
    assert o.status == OrderStatus.CANCELLED and o.frozen_cash == 0
    fees = sum(t.fee for t in broker.trades_seen)
    assert broker.cash == pytest.approx(100_000 - 300 * 10.01 - 300 * 10.02 - fees)


def test_t_plus_one(broker, tick_factory):
    broker.on_tick(tick_factory(dt=DAY1, last=10.0))
    broker.submit(Order("600000", Direction.BUY, 10.01, 1000))
    pos = broker.get_position("600000")
    assert pos.volume == 1000 and pos.available == 0
    rej = broker.submit(Order("600000", Direction.SELL, 9.99, 1000))
    assert rej.status == OrderStatus.REJECTED and "可卖" in rej.reason
    broker.on_tick(tick_factory(dt=DAY2, last=10.5, pre_close=10.0))
    assert broker.get_position("600000").available == 1000
    o = broker.submit(Order("600000", Direction.SELL, 10.5, 1000))
    assert o.status == OrderStatus.FILLED
    t = broker.trades_seen[-1]
    assert t.stamp_duty == pytest.approx(10.5 * 1000 * 0.0005, abs=0.01)
    assert broker.get_position("600000").volume == 0


def test_price_band_and_limit_up(broker, tick_factory):
    tick = tick_factory(dt=DAY1, last=11.0, asks=[0, 0, 0, 0, 0], ask_vols=[0] * 5, up=11.0, down=9.0)
    broker.on_tick(tick)
    assert broker.submit(Order("600000", Direction.BUY, 11.5, 100)).status == OrderStatus.REJECTED
    o = broker.submit(Order("600000", Direction.BUY, 11.0, 100))
    assert o.status == OrderStatus.SUBMITTED  # 涨停封板，排队买不进
    broker.on_tick(tick_factory(dt=DAY1.replace(minute=1), last=10.98, asks=[10.99], ask_vols=[500], up=11.0, down=9.0))
    assert o.status == OrderStatus.FILLED and o.avg_price == 10.99


def test_market_order_uses_limit_price_as_protection(broker, tick_factory):
    broker.on_tick(tick_factory(dt=DAY1, last=10.0))
    o = broker.submit(Order("600000", Direction.BUY, 0, 100, OrderType.MARKET))
    assert o.price == 11.0 and o.status == OrderStatus.FILLED and o.avg_price == 10.01


def test_insufficient_cash(broker, tick_factory):
    broker.on_tick(tick_factory(dt=DAY1, last=10.0))
    o = broker.submit(Order("600000", Direction.BUY, 10.0, 20000))
    assert o.status == OrderStatus.REJECTED and "资金" in o.reason


def test_orders_expire_on_new_day(broker, tick_factory):
    broker.on_tick(tick_factory(dt=DAY1, last=10.0))
    o = broker.submit(Order("600000", Direction.BUY, 9.5, 100))
    assert o.status == OrderStatus.SUBMITTED
    broker.on_tick(tick_factory(dt=DAY2, last=10.0))
    assert o.status == OrderStatus.CANCELLED and broker.cash == 100_000


def test_daily_bar_matching():
    b = PaperBroker(100_000, expire_on_new_day=False)
    b.set_time(DAY1)
    b.set_limits("600000", 10.0)
    o1 = b.submit(Order("600000", Direction.BUY, 10.5, 100))
    o2 = b.submit(Order("600000", Direction.BUY, 9.8, 100))
    o3 = b.submit(Order("600000", Direction.BUY, 9.0, 100))
    b.match_bar(Bar("600000", DAY1, "1d", 10.2, 10.6, 9.7, 10.4, 1_000_000, 0, 10.0))
    assert o1.avg_price == 10.2       # 开盘价优于委托价，按开盘价
    assert o2.avg_price == 9.8        # 盘中触及，按委托价
    assert o3.status == OrderStatus.SUBMITTED
    # 一字涨停买不进
    b2 = PaperBroker(100_000, expire_on_new_day=False)
    b2.set_time(DAY1)
    b2.set_limits("600000", 10.0)
    o = b2.submit(Order("600000", Direction.BUY, 11.0, 100))
    b2.match_bar(Bar("600000", DAY1, "1d", 11.0, 11.0, 11.0, 11.0, 100_000, 0, 10.0))
    assert o.status == OrderStatus.SUBMITTED
