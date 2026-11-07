"""基于 AKShare（东方财富 push2 接口）的准实时行情源。

AKShare 本质是 HTTP 轮询，不是交易所推送，所以「实时流」的关键在于：
  1. 选对接口：
     - quote 模式：`stock_bid_ask_em` 逐只拉取五档盘口 + 最新价 + 累计成交量，单次请求约 100~300ms，
       适合几十只以内的自选股，轮询间隔 3s 左右；
     - spot 模式：`stock_zh_a_spot_em` 拉全市场 5000+ 只的快照，但 akshare 会分 50+ 页、页间随机休眠，
       一轮约 1 分钟，只适合做选股扫描，不适合盯盘交易。
  2. 控制频率：东方财富对同一 IP 有限流（断开连接），触发后至少 5 分钟、反复触发 40 分钟以上。
     这里用并发上限 + 轮询间隔 + 指数退避保护 IP。
  3. 去重：两次轮询之间没有新成交、盘口没变就不发事件，下游只处理真正的变化。
  4. 把阻塞的 HTTP 请求丢进线程池（asyncio.to_thread），事件循环里只做轻量处理。

注意：东方财富会把境外 IP 重定向到延时行情或直接断开，请在中国大陆网络环境运行。
真正毫秒级的推送行情需要券商终端（如 miniQMT 的 xtdata.subscribe_quote），见 qmt_feed.py。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from ..core.calendar import TradingCalendar, in_market_hours, now_cn
from ..core.events import EventBus
from ..core.models import Tick
from ..core.rules import limit_prices
from .base import DataFeed

log = logging.getLogger(__name__)

try:  # akshare >= 1.17 才有专门的限流异常
    from akshare.utils.http import Push2RateLimitError  # type: ignore
except Exception:  # noqa: BLE001
    class Push2RateLimitError(Exception):  # type: ignore[no-redef]
        pass


def _num(value: Any) -> float:
    try:
        if value is None or value == "-" or value == "":
            return 0.0
        v = float(value)
        return 0.0 if v != v else v  # NaN -> 0
    except (TypeError, ValueError):
        return 0.0


def parse_bid_ask(symbol: str, data: dict[str, Any], dt: datetime, name: str = "") -> Tick | None:
    """把 ak.stock_bid_ask_em 的 item/value 字典转成 Tick。停牌或尚未开盘（无最新价）返回 None。"""
    last = _num(data.get("最新"))
    pre_close = _num(data.get("昨收"))
    if last <= 0:
        return None
    up, down = _num(data.get("涨停")), _num(data.get("跌停"))
    if up <= 0 or down <= 0:
        up, down = limit_prices(symbol, pre_close, name)
    return Tick(
        symbol=symbol,
        name=name,
        dt=dt,
        last=last,
        open=_num(data.get("今开")),
        high=_num(data.get("最高")),
        low=_num(data.get("最低")),
        pre_close=pre_close,
        volume=int(_num(data.get("总手")) * 100),   # 手 -> 股
        amount=_num(data.get("金额")),
        limit_up=up,
        limit_down=down,
        bid_prices=[_num(data.get(f"buy_{i}")) for i in range(1, 6)],
        bid_volumes=[int(_num(data.get(f"buy_{i}_vol"))) for i in range(1, 6)],   # akshare 已换算为股
        ask_prices=[_num(data.get(f"sell_{i}")) for i in range(1, 6)],
        ask_volumes=[int(_num(data.get(f"sell_{i}_vol"))) for i in range(1, 6)],
        source="akshare",
    )


def parse_spot_row(row: dict[str, Any], dt: datetime) -> Tick | None:
    """把 ak.stock_zh_a_spot_em 的一行转成 Tick（无盘口）。"""
    symbol = str(row.get("代码", "")).zfill(6)
    last = _num(row.get("最新价"))
    if not symbol or last <= 0:
        return None
    name = str(row.get("名称", ""))
    pre_close = _num(row.get("昨收"))
    up, down = limit_prices(symbol, pre_close, name)
    return Tick(
        symbol=symbol,
        name=name,
        dt=dt,
        last=last,
        open=_num(row.get("今开")),
        high=_num(row.get("最高")),
        low=_num(row.get("最低")),
        pre_close=pre_close,
        volume=int(_num(row.get("成交量")) * 100),
        amount=_num(row.get("成交额")),
        limit_up=up,
        limit_down=down,
        source="akshare-spot",
    )


class AkshareFeed(DataFeed):
    source = "akshare"

    def __init__(
        self,
        bus: EventBus,
        symbols: list[str],
        mode: str = "quote",
        interval: float = 3.0,
        max_concurrency: int = 4,
        only_trading_hours: bool = True,
        idle_interval: float = 30.0,
        ban_backoff: float = 300.0,
        max_ban_backoff: float = 2400.0,
        names: dict[str, str] | None = None,
        calendar: TradingCalendar | None = None,
    ):
        super().__init__(bus, symbols)
        if mode not in ("quote", "spot"):
            raise ValueError("mode 只能是 quote 或 spot")
        self.mode = mode
        self.interval = max(interval, 1.0 if mode == "quote" else 30.0)
        self.max_concurrency = max(1, max_concurrency)
        self.only_trading_hours = only_trading_hours
        self.idle_interval = idle_interval
        self.base_ban_backoff = ban_backoff
        self.max_ban_backoff = max_ban_backoff
        self._ban_backoff = ban_backoff
        self._ban_until: datetime | None = None
        self.names: dict[str, str] = dict(names or {})
        self.calendar = calendar or TradingCalendar()
        self._last_key: dict[str, tuple] = {}
        self._ak = None

    # ---------------------------------------------------------------- helpers
    @property
    def ak(self):
        if self._ak is None:
            import akshare as ak  # 延迟导入，仿真模式不依赖 akshare

            self._ak = ak
        return self._ak

    def market_open(self, now: datetime) -> bool:
        return self.calendar.is_trading_day(now.date()) and in_market_hours(now.time())

    def _changed(self, tick: Tick) -> bool:
        key = (tick.last, tick.volume, tick.bid1, tick.ask1,
               tick.bid_volumes[0] if tick.bid_volumes else 0,
               tick.ask_volumes[0] if tick.ask_volumes else 0)
        if self._last_key.get(tick.symbol) == key:
            return False
        self._last_key[tick.symbol] = key
        return True

    def _fetch_quote(self, symbol: str) -> dict[str, Any]:
        df = self.ak.stock_bid_ask_em(symbol=symbol)
        return dict(zip(df["item"], df["value"]))

    def _fetch_spot(self) -> list[dict[str, Any]]:
        df = self.ak.stock_zh_a_spot_em()
        return df.to_dict("records")

    # ---------------------------------------------------------------- polling
    async def poll_once(self) -> int:
        """轮询一次，返回发布的 Tick 数。限流异常向上抛出。"""
        t0 = time.monotonic()
        published = 0
        if self.mode == "quote":
            sem = asyncio.Semaphore(self.max_concurrency)
            rate_limited: list[Exception] = []

            async def one(sym: str) -> Tick | None:
                async with sem:
                    if rate_limited:
                        return None  # 已被限流，后面的请求不要再发
                    try:
                        data = await asyncio.to_thread(self._fetch_quote, sym)
                    except Push2RateLimitError as exc:
                        rate_limited.append(exc)
                        return None
                    except Exception as exc:  # noqa: BLE001
                        self.stats["errors"] += 1
                        log.warning("获取 %s 行情失败: %s", sym, exc)
                        return None
                    return parse_bid_ask(sym, data, now_cn(), self.names.get(sym, ""))

            ticks = await asyncio.gather(*(one(s) for s in list(self.symbols)))
            if rate_limited:
                raise rate_limited[0]
        else:
            rows = await asyncio.to_thread(self._fetch_spot)
            dt = now_cn()
            wanted = set(self.symbols)
            ticks = []
            for row in rows:
                tick = parse_spot_row(row, dt)
                if tick and (not wanted or tick.symbol in wanted):
                    self.names.setdefault(tick.symbol, tick.name)
                    ticks.append(tick)

        for tick in ticks:
            if tick is not None and self._changed(tick):
                self.emit(tick)
                published += 1
        self.stats["polls"] += 1
        self.stats["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return published

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        first = True
        while self._running:
            now = now_cn()
            if self._ban_until and now < self._ban_until:
                await self.sleep(min((self._ban_until - now).total_seconds(), self.idle_interval))
                continue
            # 非交易时段：启动时仍拉一次，方便界面显示最新价；之后低频等待
            if self.only_trading_hours and not first and not self.market_open(now):
                self.set_state("closed", "当前非交易时段，暂停轮询")
                await self.sleep(self.idle_interval)
                continue
            first = False
            started = time.monotonic()
            try:
                await self.poll_once()
                self._ban_backoff = self.base_ban_backoff
                self.set_state("running")
            except Push2RateLimitError:
                self._ban_until = now_cn() + timedelta(seconds=self._ban_backoff)
                self.set_state(
                    "rate_limited",
                    f"东方财富限流（或当前 IP 不在中国大陆），暂停 {int(self._ban_backoff)} 秒。"
                    "可调大 interval 或减少自选股数量",
                )
                self._ban_backoff = min(self._ban_backoff * 2, self.max_ban_backoff)
                continue
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.set_state("error", f"行情轮询失败: {exc}")
                await self.sleep(min(self.interval * 3, 30))
                continue
            elapsed = time.monotonic() - started
            await self.sleep(max(self.interval - elapsed, 0.2))

    def status(self) -> dict[str, Any]:
        st = super().status()
        st.update(mode=self.mode, interval=self.interval,
                  ban_until=self._ban_until.isoformat(timespec="seconds") if self._ban_until else None)
        return st
