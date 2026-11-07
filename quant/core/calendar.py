"""交易时段与交易日历。

- 集合竞价 9:15-9:25，连续竞价 9:30-11:30、13:00-15:00
- 交易日：优先读取 akshare 的新浪交易日历（缓存到本地），取不到时退化为「周一到周五」
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")

AUCTION_START = time(9, 15)
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)


def now_cn() -> datetime:
    """当前北京时间（naive）。"""
    return datetime.now(CN_TZ).replace(tzinfo=None)


def in_continuous_session(t: time) -> bool:
    return MORNING_OPEN <= t < MORNING_CLOSE or AFTERNOON_OPEN <= t < AFTERNOON_CLOSE


def in_market_hours(t: time, grace_seconds: int = 60) -> bool:
    """含集合竞价与收盘后少许缓冲，用于决定是否继续轮询行情。"""
    end_am = (datetime.combine(date.min, MORNING_CLOSE) + timedelta(seconds=grace_seconds)).time()
    end_pm = (datetime.combine(date.min, AFTERNOON_CLOSE) + timedelta(seconds=grace_seconds)).time()
    return AUCTION_START <= t <= end_am or AFTERNOON_OPEN <= t <= end_pm


def bar_minute(dt: datetime) -> datetime | None:
    """把行情时间映射到所属 1 分钟 K 线的起始时间（行业通行做法）。

    - 9:25~9:30 的开盘集合竞价成交归入 9:30 这根
    - 11:30:xx 归入 11:29，15:00:xx 归入 14:59（收盘集合竞价）
    - 其余非交易时段返回 None
    """
    t = dt.time()
    base = dt.replace(second=0, microsecond=0)
    if time(9, 25) <= t < MORNING_OPEN:
        return base.replace(hour=9, minute=30)
    if in_continuous_session(t):
        return base
    if MORNING_CLOSE <= t < time(11, 31):
        return base.replace(hour=11, minute=29)
    if AFTERNOON_CLOSE <= t < time(15, 1):
        return base.replace(hour=14, minute=59)
    return None


def minutes_since_open(dt: datetime) -> int:
    """自 9:30 起的交易分钟序号（0~239），午休不计。"""
    t = dt.hour * 60 + dt.minute
    if t < 9 * 60 + 30:
        return 0
    if t < 11 * 60 + 30:
        return t - (9 * 60 + 30)
    if t < 13 * 60:
        return 120
    return min(120 + t - 13 * 60, 239)


class TradingCalendar:
    def __init__(self, cache_path: str | Path = "data/trade_dates.json"):
        self.cache_path = Path(cache_path)
        self._dates: set[date] | None = None

    def _load(self) -> set[date] | None:
        if self._dates is not None:
            return self._dates
        try:
            if self.cache_path.exists():
                raw = json.loads(self.cache_path.read_text())
                self._dates = {date.fromisoformat(d) for d in raw}
                return self._dates
        except Exception as exc:  # noqa: BLE001
            log.warning("读取交易日历缓存失败: %s", exc)
        return None

    def refresh(self) -> bool:
        """从 akshare 拉取交易日历（需要能访问新浪财经）。"""
        try:
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
            dates = sorted({str(d) for d in df["trade_date"]})
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(dates))
            self._dates = {date.fromisoformat(d) for d in dates}
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("拉取交易日历失败，退化为工作日判断: %s", exc)
            return False

    def is_trading_day(self, d: date) -> bool:
        dates = self._load()
        if dates and min(dates) <= d <= max(dates):
            return d in dates
        return d.weekday() < 5
