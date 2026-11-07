"""DuckDB 本地存储：K 线、行情快照、委托、成交。

DuckDB 是嵌入式列存数据库（单文件、免安装），按 symbol/时间范围查 K 线非常快，也能直接用 SQL 做研究。
同一个连接不支持并发写，这里用一把锁串行化；实时写入由 StoreWriter 批量异步完成，不阻塞行情链路。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from ..core.events import BAR, ORDER, TICK, TRADE, EventBus
from ..core.models import Bar, Order, Tick, Trade

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol VARCHAR, interval VARCHAR, dt TIMESTAMP,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT, amount DOUBLE, pre_close DOUBLE,
    PRIMARY KEY (symbol, interval, dt)
);
CREATE TABLE IF NOT EXISTS ticks (
    symbol VARCHAR, dt TIMESTAMP, last DOUBLE, volume BIGINT, amount DOUBLE,
    bid1 DOUBLE, bid1_vol BIGINT, ask1 DOUBLE, ask1_vol BIGINT, source VARCHAR
);
CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR PRIMARY KEY, created TIMESTAMP, updated TIMESTAMP, symbol VARCHAR, direction VARCHAR,
    order_type VARCHAR, price DOUBLE, volume BIGINT, traded BIGINT, avg_price DOUBLE,
    status VARCHAR, strategy VARCHAR, reason VARCHAR
);
CREATE TABLE IF NOT EXISTS trades (
    id VARCHAR PRIMARY KEY, order_id VARCHAR, dt TIMESTAMP, symbol VARCHAR, direction VARCHAR,
    price DOUBLE, volume BIGINT, commission DOUBLE, stamp_duty DOUBLE, transfer_fee DOUBLE, strategy VARCHAR
);
"""

BAR_COLS = ["symbol", "interval", "dt", "open", "high", "low", "close", "volume", "amount", "pre_close"]


class Store:
    def __init__(self, path: str | Path = "data/quant.duckdb"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(self.path)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------- writes
    def write_bars_df(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        df = df[BAR_COLS].copy()
        with self.lock:
            self.conn.register("_bars_df", df)
            self.conn.execute("INSERT OR REPLACE INTO bars SELECT * FROM _bars_df")
            self.conn.unregister("_bars_df")
        return len(df)

    def write_bars(self, bars: list[Bar]) -> int:
        if not bars:
            return 0
        df = pd.DataFrame([[b.symbol, b.interval, b.dt, b.open, b.high, b.low, b.close, b.volume, b.amount, b.pre_close] for b in bars], columns=BAR_COLS)
        return self.write_bars_df(df)

    def write_ticks(self, ticks: list[Tick]) -> int:
        if not ticks:
            return 0
        df = pd.DataFrame(
            [[t.symbol, t.dt, t.last, t.volume, t.amount, t.bid1, t.bid_volumes[0] if t.bid_volumes else 0,
              t.ask1, t.ask_volumes[0] if t.ask_volumes else 0, t.source] for t in ticks],
            columns=["symbol", "dt", "last", "volume", "amount", "bid1", "bid1_vol", "ask1", "ask1_vol", "source"],
        )
        with self.lock:
            self.conn.register("_ticks_df", df)
            self.conn.execute("INSERT INTO ticks SELECT * FROM _ticks_df")
            self.conn.unregister("_ticks_df")
        return len(df)

    def write_orders(self, orders: list[Order]) -> None:
        if not orders:
            return
        rows = [[o.id, o.created, o.updated, o.symbol, o.direction.value, o.order_type.value, o.price, o.volume,
                 o.traded, o.avg_price, o.status.value, o.strategy, o.reason] for o in orders]
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def write_trades(self, trades: list[Trade]) -> None:
        if not trades:
            return
        rows = [[t.id, t.order_id, t.dt, t.symbol, t.direction.value, t.price, t.volume, t.commission,
                 t.stamp_duty, t.transfer_fee, t.strategy] for t in trades]
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    # ------------------------------------------------------------- reads
    def load_bars(self, symbol: str, interval: str = "1d", start: datetime | str | None = None,
                  end: datetime | str | None = None, limit: int | None = None) -> pd.DataFrame:
        sql = "SELECT * FROM bars WHERE symbol = ? AND interval = ?"
        params: list = [symbol, interval]
        if start is not None:
            sql += " AND dt >= ?"
            params.append(pd.Timestamp(start).to_pydatetime())
        if end is not None:
            sql += " AND dt <= ?"
            params.append(pd.Timestamp(end).to_pydatetime())
        if limit:
            sql = f"SELECT * FROM ({sql} ORDER BY dt DESC LIMIT {int(limit)}) ORDER BY dt"
        else:
            sql += " ORDER BY dt"
        with self.lock:
            return self.conn.execute(sql, params).df()

    def available(self, interval: str = "1d") -> pd.DataFrame:
        with self.lock:
            return self.conn.execute(
                "SELECT symbol, count(*) AS bars, min(dt) AS start, max(dt) AS end FROM bars WHERE interval = ? GROUP BY symbol ORDER BY symbol",
                [interval],
            ).df()

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        with self.lock:
            return self.conn.execute(sql, params or []).df()


class StoreWriter:
    """订阅事件总线，批量异步落盘。"""

    def __init__(self, bus: EventBus, store: Store, store_ticks: bool = True, flush_interval: float = 1.0):
        self.store = store
        self.store_ticks = store_ticks
        self.flush_interval = flush_interval
        topics = [BAR, ORDER, TRADE] + ([TICK] if store_ticks else [])
        self.sub = bus.stream(topics, maxsize=50000)
        self._running = False
        self.written = {"ticks": 0, "bars": 0}

    async def run(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self.flush_interval)
            await self.flush()

    async def flush(self) -> None:
        ticks: list[Tick] = []
        bars: list[Bar] = []
        orders: dict[str, Order] = {}
        trades: list[Trade] = []
        q = self.sub.queue
        while not q.empty():
            ev = q.get_nowait()
            if ev.topic == TICK:
                ticks.append(ev.data)
            elif ev.topic == BAR:
                bars.append(ev.data)
            elif ev.topic == ORDER:
                orders[ev.data.id] = ev.data
            elif ev.topic == TRADE:
                trades.append(ev.data)
        if not (ticks or bars or orders or trades):
            return
        try:
            await asyncio.to_thread(self._write, ticks, bars, list(orders.values()), trades)
        except Exception:  # noqa: BLE001
            log.exception("写入数据库失败")

    def _write(self, ticks, bars, orders, trades) -> None:
        self.written["ticks"] += self.store.write_ticks(ticks)
        self.written["bars"] += self.store.write_bars(bars)
        self.store.write_orders(orders)
        self.store.write_trades(trades)

    def stop(self) -> None:
        self._running = False
        self.sub.close()
