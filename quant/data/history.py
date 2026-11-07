"""历史数据：用 akshare 下载日线 / 分钟线入库；离线时可生成合成数据用于联调。"""
from __future__ import annotations

import logging
import math
import random
import time
from datetime import datetime

import pandas as pd

from ..core.rules import limit_prices, round_price
from .store import BAR_COLS, Store

log = logging.getLogger(__name__)


def akshare_daily_to_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """ak.stock_zh_a_hist 的结果 -> bars 表格式。昨收 = 收盘 - 涨跌额（比 shift 更准，停牌也不会错位）。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=BAR_COLS)
    out = pd.DataFrame({
        "symbol": symbol,
        "interval": "1d",
        "dt": pd.to_datetime(df["日期"]),
        "open": df["开盘"].astype(float),
        "high": df["最高"].astype(float),
        "low": df["最低"].astype(float),
        "close": df["收盘"].astype(float),
        "volume": (df["成交量"].astype(float) * 100).astype("int64"),  # 手 -> 股
        "amount": df["成交额"].astype(float),
        "pre_close": (df["收盘"].astype(float) - df["涨跌额"].astype(float)).round(2),
    })
    return out[BAR_COLS]


def akshare_minute_to_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """ak.stock_zh_a_hist_min_em(period='1') 的结果 -> bars 表格式。

    东方财富分钟线的时间戳是 K 线「结束」时间（09:31 表示 9:30~9:31），这里统一改为起始时间，
    并把 09:30 那根集合竞价线并入 9:30 这一分钟。
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=BAR_COLS)
    end = pd.to_datetime(df["时间"])
    start = end - pd.Timedelta(minutes=1)
    start = start.where(~((start.dt.hour == 9) & (start.dt.minute < 30)), start.dt.normalize() + pd.Timedelta(hours=9, minutes=30))
    raw = pd.DataFrame({
        "dt": start, "open": df["开盘"].astype(float), "high": df["最高"].astype(float),
        "low": df["最低"].astype(float), "close": df["收盘"].astype(float),
        "volume": df["成交量"].astype(float) * 100, "amount": df["成交额"].astype(float),
    })
    g = raw.groupby("dt", sort=True).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                          close=("close", "last"), volume=("volume", "sum"), amount=("amount", "sum")).reset_index()
    g["symbol"], g["interval"], g["pre_close"] = symbol, "1m", 0.0
    g["volume"] = g["volume"].astype("int64")
    return g[BAR_COLS]


def download_daily(store: Store, symbols: list[str], start: str = "20200101", end: str | None = None,
                   adjust: str = "qfq", pause: float = 1.0) -> dict[str, int]:
    """下载日线入库。adjust: qfq 前复权（回测推荐） / hfq 后复权 / '' 不复权。"""
    import akshare as ak

    end = end or datetime.now().strftime("%Y%m%d")
    result: dict[str, int] = {}
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(pause)  # 保护 IP，别被限流
        try:
            df = ak.stock_zh_a_hist(symbol=sym, period="daily", start_date=start, end_date=end, adjust=adjust)
            result[sym] = store.write_bars_df(akshare_daily_to_bars(df, sym))
            log.info("%s 日线 %d 条", sym, result[sym])
        except Exception as exc:  # noqa: BLE001
            log.error("下载 %s 日线失败: %s", sym, exc)
            result[sym] = -1
    return result


def download_minute(store: Store, symbols: list[str], pause: float = 1.0) -> dict[str, int]:
    """下载最近几个交易日的 1 分钟线（东方财富只提供近 5 个交易日左右）。"""
    import akshare as ak

    result: dict[str, int] = {}
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(pause)
        try:
            df = ak.stock_zh_a_hist_min_em(symbol=sym, period="1", adjust="")
            result[sym] = store.write_bars_df(akshare_minute_to_bars(df, sym))
        except Exception as exc:  # noqa: BLE001
            log.error("下载 %s 分钟线失败: %s", sym, exc)
            result[sym] = -1
    return result


def make_synthetic_daily(symbol: str, start: str = "2022-01-01", end: str = "2025-12-31",
                         price: float = 20.0, annual_vol: float = 0.35, seed: int | None = None) -> pd.DataFrame:
    """生成遵守涨跌停的合成日线（带趋势段），仅用于离线联调/测试，不代表真实行情。"""
    rng = random.Random(seed if seed is not None else hash(symbol) & 0xFFFF)
    days = pd.bdate_range(start, end)
    sigma = annual_vol / math.sqrt(252)
    rows, pre, drift, left = [], price, 0.0, 0
    for d in days:
        if left <= 0:
            drift, left = rng.gauss(0, sigma * 0.12), rng.randint(15, 60)
        left -= 1
        up, down = limit_prices(symbol, pre)
        o = min(max(round_price(pre * math.exp(rng.gauss(0, sigma * 0.3))), down), up)
        c = min(max(round_price(pre * math.exp(drift + rng.gauss(0, sigma))), down), up)
        h = min(round_price(max(o, c) * (1 + abs(rng.gauss(0, sigma * 0.5)))), up)
        lo = max(round_price(min(o, c) * (1 - abs(rng.gauss(0, sigma * 0.5)))), down)
        vol = int(rng.lognormvariate(math.log(5e6), 0.5)) // 100 * 100
        rows.append([symbol, "1d", d.to_pydatetime(), o, h, lo, c, vol, round(vol * (o + c) / 2, 2), pre])
        pre = c
    return pd.DataFrame(rows, columns=BAR_COLS)


def ensure_synthetic(store: Store, symbols: list[str], **kw) -> None:
    for sym in symbols:
        if store.load_bars(sym, "1d", limit=1).empty:
            store.write_bars_df(make_synthetic_daily(sym, **kw))
