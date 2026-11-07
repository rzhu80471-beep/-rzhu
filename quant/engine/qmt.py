"""miniQMT（迅投 xtquant）适配器：真正的推送行情 + 实盘交易。⚠️ 未经实盘验证，请先用模拟账户测试。

为什么需要它：akshare 是 HTTP 轮询（秒级、有限流），而券商提供的 miniQMT 通过本地客户端
推送逐笔快照（xtdata.subscribe_quote / subscribe_whole_quote 全市场推送），延迟低、无限流，
并且是个人投资者合规接入程序化实盘的主流方式（需向券商申请开通，通常有资产门槛）。

使用前提：
1. Windows 上安装券商版 QMT，以「极简模式 / 独立交易」登录（即 miniQMT）
2. `pip install xtquant`（或使用券商提供的 xtquant 包）
3. config.yaml 中设置 feed.type: qmt、broker.type: qmt，并填写 userdata_mini 路径和资金账号

xtquant 的回调在它自己的线程里触发，这里统一通过 bus.publish_threadsafe 切回事件循环线程。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from ..core.events import EventBus
from ..core.models import AccountSnapshot, Direction, Order, OrderStatus, OrderType, Position, Tick, Trade
from ..core.rules import exchange_of, limit_prices
from ..data.base import DataFeed
from .broker import Broker

log = logging.getLogger(__name__)

VOLUME_MULTIPLIER = 100  # xtdata 股票 tick 的 volume / askVol / bidVol 单位为「手」


def to_qmt_code(symbol: str) -> str:
    return f"{symbol}.{exchange_of(symbol)}"


def from_qmt_code(code: str) -> str:
    return code.split(".")[0]


def parse_qmt_tick(code: str, d: dict[str, Any]) -> Tick | None:
    last = float(d.get("lastPrice") or 0)
    if last <= 0:
        return None
    symbol = from_qmt_code(code)
    pre = float(d.get("lastClose") or 0)
    up, down = limit_prices(symbol, pre)
    ts = d.get("time")
    dt = datetime.fromtimestamp(ts / 1000) if ts else datetime.now()
    return Tick(
        symbol=symbol, dt=dt, last=last, open=float(d.get("open") or 0), high=float(d.get("high") or 0),
        low=float(d.get("low") or 0), pre_close=pre, volume=int(d.get("volume") or 0) * VOLUME_MULTIPLIER,
        amount=float(d.get("amount") or 0), limit_up=up, limit_down=down,
        bid_prices=[float(x) for x in (d.get("bidPrice") or [])][:5],
        bid_volumes=[int(x) * VOLUME_MULTIPLIER for x in (d.get("bidVol") or [])][:5],
        ask_prices=[float(x) for x in (d.get("askPrice") or [])][:5],
        ask_volumes=[int(x) * VOLUME_MULTIPLIER for x in (d.get("askVol") or [])][:5],
        source="qmt",
    )


class QmtFeed(DataFeed):
    source = "qmt"

    def __init__(self, bus: EventBus, symbols: list[str], period: str = "tick", whole_market: bool = False):
        super().__init__(bus, symbols)
        self.period = period
        self.whole_market = whole_market
        self._seqs: list[int] = []

    def _on_data(self, datas: dict[str, Any]) -> None:  # xtquant 线程
        for code, payload in datas.items():
            items = payload if isinstance(payload, list) else [payload]
            for d in items:
                tick = parse_qmt_tick(code, d)
                if tick:
                    self.bus.publish_threadsafe("__qmt_tick", tick)

    def _subscribe(self) -> None:
        from xtquant import xtdata  # type: ignore

        if self.whole_market:
            self._seqs.append(xtdata.subscribe_whole_quote(["SH", "SZ", "BJ"], callback=self._on_data))
        else:
            for sym in self.symbols:
                self._seqs.append(xtdata.subscribe_quote(to_qmt_code(sym), period=self.period, count=0, callback=self._on_data))

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        self.bus.on("__qmt_tick", self.emit)
        try:
            await asyncio.to_thread(self._subscribe)
            self.set_state("running")
        except Exception as exc:  # noqa: BLE001
            self.set_state("error", f"订阅 QMT 行情失败: {exc}")
            return
        while self._running:
            await self.sleep(1.0)

    def stop(self) -> None:
        super().stop()
        try:
            from xtquant import xtdata  # type: ignore

            for seq in self._seqs:
                xtdata.unsubscribe_quote(seq)
        except Exception:  # noqa: BLE001
            pass
        self._seqs.clear()


class QmtBroker(Broker):
    """把 xttrader 的委托/成交回报映射为本系统的 Order / Trade。持仓和资金以券商查询为准。"""

    name = "qmt"

    def __init__(self, path: str, account_id: str, session_id: int = 0, account_type: str = "STOCK"):
        super().__init__()
        if not path or not account_id:
            raise ValueError("QMT 需要配置 broker.qmt.path（userdata_mini 目录）和 account_id")
        self.path, self.account_id, self.account_type = path, account_id, account_type
        self.session_id = session_id or int(datetime.now().timestamp()) % 100000
        self.bus: EventBus | None = None
        self._orders: dict[str, Order] = {}
        self._by_broker_id: dict[int, Order] = {}
        self._trades: list[Trade] = []
        self._positions: dict[str, Position] = {}
        self._account = AccountSnapshot(None, 0, 0, 0, 0, 0, 0)
        self._last: dict[str, float] = {}

    async def start(self, bus: EventBus) -> None:
        from xtquant import xtconstant  # type: ignore
        from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback  # type: ignore
        from xtquant.xttype import StockAccount  # type: ignore

        self.bus, self._c = bus, xtconstant

        class _Callback(XtQuantTraderCallback):  # 运行在 xtquant 线程
            def on_stock_order(self, o):
                bus.publish_threadsafe("__qmt_order", o)

            def on_stock_trade(self, t):
                bus.publish_threadsafe("__qmt_trade", t)

            def on_order_error(self, e):
                bus.publish_threadsafe("__qmt_order_error", e)

            def on_disconnected(self):
                bus.publish_threadsafe("log", {"level": "error", "source": "qmt", "message": "与 miniQMT 断开连接"})

        bus.on("__qmt_order", self._handle_order)
        bus.on("__qmt_trade", self._handle_trade)
        bus.on("__qmt_order_error", self._handle_error)
        self.trader = XtQuantTrader(self.path, self.session_id)
        self.trader.register_callback(_Callback())
        self.trader.start()
        if self.trader.connect() != 0:
            raise RuntimeError("连接 miniQMT 失败，请确认客户端已以极简模式登录且路径正确")
        self.acc = StockAccount(self.account_id, self.account_type)
        self.trader.subscribe(self.acc)
        self.sync()
        asyncio.get_running_loop().create_task(self._sync_loop())

    async def _sync_loop(self) -> None:
        while True:
            await asyncio.sleep(3)
            try:
                await asyncio.to_thread(self.sync)
            except Exception:  # noqa: BLE001
                log.exception("同步 QMT 资金持仓失败")

    def sync(self) -> None:
        asset = self.trader.query_stock_asset(self.acc)
        if asset:
            self._account = AccountSnapshot(datetime.now(), asset.cash, asset.frozen_cash, asset.market_value,
                                            asset.total_asset, self._account.initial_cash or asset.total_asset,
                                            self._account.day_start_equity or asset.total_asset)
        positions = {}
        for p in self.trader.query_stock_positions(self.acc) or []:
            sym = from_qmt_code(p.stock_code)
            pos = Position(sym, volume=p.volume, cost=p.open_price, last_price=self._last.get(sym, p.open_price))
            pos.today_bought = max(p.volume - p.can_use_volume - getattr(p, "frozen_volume", 0), 0)
            pos.frozen = getattr(p, "frozen_volume", 0)
            positions[sym] = pos
        self._positions = positions

    def stop(self) -> None:
        try:
            self.trader.stop()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- Broker API
    def submit(self, order: Order) -> Order:
        c = self._c
        side = c.STOCK_BUY if order.direction == Direction.BUY else c.STOCK_SELL
        if order.order_type == OrderType.MARKET:  # 最优五档即时成交剩余撤销
            price_type = c.MARKET_SH_CONVERT_5_CANCEL if exchange_of(order.symbol) == "SH" else c.MARKET_SZ_CONVERT_5_CANCEL
            price = 0.0
        else:
            price_type, price = c.FIX_PRICE, order.price
        order.created = order.updated = datetime.now()
        broker_id = self.trader.order_stock(self.acc, to_qmt_code(order.symbol), side, order.volume, price_type, price, order.strategy, order.id)
        if broker_id is None or broker_id < 0:
            order.status, order.reason = OrderStatus.REJECTED, "QMT 下单失败"
        else:
            order.broker_order_id = str(broker_id)
            self._by_broker_id[broker_id] = order
        self._orders[order.id] = order
        self.on_order(order)
        return order

    def cancel(self, order_id: str, reason: str = "用户撤单") -> bool:
        order = self._orders.get(order_id)
        if not order or not order.broker_order_id:
            return False
        return self.trader.cancel_order_stock(self.acc, int(order.broker_order_id)) == 0

    def _handle_order(self, o: Any) -> None:
        c = self._c
        order = self._by_broker_id.get(o.order_id)
        if order is None:
            return
        status_map = {c.ORDER_SUCCEEDED: OrderStatus.FILLED, c.ORDER_PART_SUCC: OrderStatus.PARTIAL,
                      c.ORDER_CANCELED: OrderStatus.CANCELLED, c.ORDER_PART_CANCEL: OrderStatus.CANCELLED,
                      c.ORDER_JUNK: OrderStatus.REJECTED}
        order.status = status_map.get(o.order_status, order.status)
        order.traded = o.traded_volume
        order.avg_price = o.traded_price
        order.updated = datetime.now()
        self.on_order(order)

    def _handle_trade(self, t: Any) -> None:
        order = self._by_broker_id.get(t.order_id)
        trade = Trade(order.id if order else str(t.order_id), from_qmt_code(t.stock_code),
                      Direction.BUY if t.order_type == self._c.STOCK_BUY else Direction.SELL,
                      t.traded_price, t.traded_volume, datetime.fromtimestamp(t.traded_time) if t.traded_time > 1e9 else datetime.now(),
                      strategy=order.strategy if order else "qmt")
        self._trades.append(trade)
        self.on_trade(trade)

    def _handle_error(self, e: Any) -> None:
        order = self._by_broker_id.get(e.order_id)
        if order:
            order.status, order.reason = OrderStatus.REJECTED, str(e.error_msg)
            self.on_order(order)

    def on_tick(self, tick: Tick) -> None:
        self._last[tick.symbol] = tick.last
        if tick.symbol in self._positions:
            self._positions[tick.symbol].last_price = tick.last

    def get_position(self, symbol: str) -> Position:
        return self._positions.get(symbol) or Position(symbol, last_price=self._last.get(symbol, 0.0))

    def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.volume > 0]

    def account(self) -> AccountSnapshot:
        return self._account

    def orders(self, active_only: bool = False) -> list[Order]:
        return [o for o in self._orders.values() if not active_only or o.status.active]

    def trades(self) -> list[Trade]:
        return list(self._trades)
