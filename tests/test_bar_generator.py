from datetime import datetime

from quant.data.bar_generator import BarGenerator, WindowBarGenerator

D = datetime(2026, 11, 9)


def at(h, m, s=0):
    return D.replace(hour=h, minute=m, second=s)


def test_ticks_to_minute_bars(tick_factory):
    bars = []
    g = BarGenerator(bars.append)
    g.update_tick(tick_factory(dt=at(9, 25, 0), last=10.0, volume=1000))   # 集合竞价 -> 9:30
    g.update_tick(tick_factory(dt=at(9, 30, 10), last=10.2, volume=1500))
    g.update_tick(tick_factory(dt=at(9, 30, 50), last=9.9, volume=2100))
    assert bars == []
    g.update_tick(tick_factory(dt=at(9, 31, 3), last=10.1, volume=2600))
    assert len(bars) == 1
    b = bars[0]
    assert b.dt == at(9, 30) and (b.open, b.high, b.low, b.close) == (10.0, 10.2, 9.9, 9.9)
    assert b.volume == 2100  # 当日第一笔是 9:30 这根，累计量全部计入
    g.flush(at(9, 32, 3))   # 9:31 的 K 线在 9:32 + 宽限期后强制收盘
    assert len(bars) == 2 and bars[1].volume == 500


def test_midday_start_ignores_prior_volume(tick_factory):
    bars = []
    g = BarGenerator(bars.append)
    g.update_tick(tick_factory(dt=at(10, 15, 5), last=10.0, volume=5_000_000))
    g.update_tick(tick_factory(dt=at(10, 15, 40), last=10.0, volume=5_000_300))
    g.flush_all()
    assert bars[0].volume == 300


def test_lunch_close_and_day_reset(tick_factory):
    bars = []
    g = BarGenerator(bars.append)
    g.update_tick(tick_factory(dt=at(11, 29, 30), last=10.0, volume=100))
    g.update_tick(tick_factory(dt=at(11, 30, 1), last=10.1, volume=300))   # 归入 11:29
    g.update_tick(tick_factory(dt=at(13, 0, 2), last=10.2, volume=400))
    assert [b.dt for b in bars] == [at(11, 29)] and bars[0].close == 10.1
    # 第二天累计量重置
    g.update_tick(tick_factory(dt=D.replace(day=10, hour=9, minute=30, second=5), last=10.3, volume=50))
    g.flush_all()
    assert bars[-1].volume == 50


def test_window_bars(tick_factory):
    from quant.core.models import Bar

    out = []
    w = WindowBarGenerator(5, out.append)
    for i in range(10):
        m = at(9, 30 + i)
        w.update_bar(Bar("600000", m, "1m", 10 + i, 11 + i, 9 + i, 10.5 + i, 100))
    assert len(out) == 2
    assert out[0].dt == at(9, 30) and out[0].open == 10 and out[0].close == 14.5 and out[0].volume == 500
    assert out[0].high == 15 and out[0].low == 9 and out[0].interval == "5m"
