"""券商接口抽象 + 本地模拟撮合（PaperBroker）。

PaperBroker 同时服务于实时模拟盘和日线回测，保证两边规则一致：
- T+1：当日买入计入 today_bought，次日开盘前清零才可卖
- 涨跌停：委托价必须在涨跌停之间；涨停封板（卖盘为空）买不进，跌停封板（买盘为空）卖不出
- 数量：买入 100 股整数倍（科创板≥200 股），卖出零股须一次清仓
- 资金：买单按「委托价×数量+预估费用」冻结资金，成交后多退少补；卖单冻结持仓
- 当日有效：换日时自动撤销未成交委托（A 股委托均为当日有效）
- 实时撮合：买单按卖一~卖五逐档吃单（价格≤委托价），可设吃单比例模拟冲击；
  没有盘口的快照（spot 模式）按最新价撮合
- 日线撮合：开盘价优于委托价按开盘价成交，否则盘中触及委托价按委托价成交；一字板不成交
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Callable

from ..core.calendar import bar_minute
from ..core.models import (
    AccountSnapshot, Bar, Direction, Order, OrderStatus, OrderType, Position, Tick, Trade,
)
from ..core.rules import FeeModel, check_volume, limit_prices, round_price

OrderCallback = Callable[[Order], None]
TradeCallback = Callable[[Trade], None]


class Broker(ABC):
    name = "base"

    def __init__(self) -> None:
        self.on_order: OrderCallback = lambda o: None
        self.on_trade: TradeCallback = lambda t: None

    @abstractmethod
    def submit(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel(self, order_id: str, reason: str = "用户撤单") -> bool: ...

    @abstractmethod
    def on_tick(self, tick: Tick) -> None: ...

    @abstractmethod
    def get_position(self, symbol: str) -> Position: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abstractmethod
    def orders(self, active_only: bool = False) -> list[Order]: ...

    @abstractmethod
    def trades(self) -> list[Trade]: ...

    def get_order(self, order_id: str) -> Order | None:
        return next((o for o in self.orders() if o.id == order_id), None)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, initial_cash: float = 1_000_000, fees: FeeModel | None = None,
                 fill_ratio: float = 1.0, expire_on_new_day: bool = True):
        super().__init__()
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.fees = fees or FeeModel()
        self.fill_ratio = fill_ratio            # 每档最多吃掉的盘口比例，<1 用于模拟冲击成本
        self.expire_on_new_day = expire_on_new_day
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._active: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._ticks: dict[str, Tick] = {}
        self._limits: dict[str, tuple[float, float]] = {}
        self._now: datetime | None = None
        self.cur_date: date | None = None
        self.day_start_equity = initial_cash

    # ------------------------------------------------------------- state
    def now(self) -> datetime:
        return self._now or datetime.now()

    def set_time(self, dt: datetime) -> None:
        self._now = dt
        self.roll_day(dt.date())

    def roll_day(self, d: date) -> None:
        if self.cur_date == d:
            return
        if self.cur_date is not None:
            if self.expire_on_new_day:
                for oid in list(self._active):
                    self.cancel(oid, "当日委托收盘自动失效")
            for pos in self._positions.values():
                pos.today_bought = 0
            self.day_start_equity = self.equity
        self.cur_date = d

    def get_position(self, symbol: str) -> Position:
        pos = self._positions.get(symbol)
        if pos is None:
            pos = self._positions[symbol] = Position(symbol, last_price=self._last(symbol))
        return pos

    def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.volume > 0 or p.frozen > 0]

    def orders(self, active_only: bool = False) -> list[Order]:
        return list((self._active if active_only else self._orders).values())

    def trades(self) -> list[Trade]:
        return list(self._trades)

    def _last(self, symbol: str) -> float:
        t = self._ticks.get(symbol)
        return t.last if t else 0.0

    @property
    def frozen_cash(self) -> float:
        return sum(o.frozen_cash for o in self._active.values())

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self._positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.frozen_cash + self.market_value

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(self._now, round(self.cash, 2), round(self.frozen_cash, 2),
                               round(self.market_value, 2), round(self.equity, 2),
                               self.initial_cash, round(self.day_start_equity, 2))

    def set_limits(self, symbol: str, pre_close: float, name: str = "") -> None:
        self._limits[symbol] = limit_prices(symbol, pre_close, name)

    def limits(self, symbol: str) -> tuple[float, float] | None:
        t = self._ticks.get(symbol)
        if t and t.limit_up > 0:
            return t.limit_up, t.limit_down
        return self._limits.get(symbol)

    # ------------------------------------------------------------- orders
    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reason = reason
        order.updated = self._now
        self._orders[order.id] = order
        self.on_order(order)
        return order

    def submit(self, order: Order) -> Order:
        if self._now:
            self.roll_day(self._now.date())
        order.created = order.updated = self._now
        lim = self.limits(order.symbol)
        if order.order_type == OrderType.MARKET:
            if not lim:
                return self._reject(order, "没有行情，无法下市价单")
            order.price = lim[0] if order.direction == Direction.BUY else lim[1]  # 以涨跌停价作为保护价
        order.price = round_price(order.price)
        if order.price <= 0:
            return self._reject(order, "委托价格无效")
        if lim and not (lim[1] <= order.price <= lim[0]):
            return self._reject(order, f"委托价超出涨跌停范围 [{lim[1]}, {lim[0]}]")
        pos = self.get_position(order.symbol)
        reason = check_volume(order.symbol, order.direction, order.volume, pos.available)
        if reason:
            return self._reject(order, reason)
        if order.direction == Direction.BUY:
            need = order.price * order.volume + self.fees.total(order.symbol, Direction.BUY, order.price, order.volume) + self.fees.min_commission
            if need > self.cash + 1e-6:
                return self._reject(order, f"可用资金不足（需要 {need:,.2f}，可用 {self.cash:,.2f}）")
            self.cash -= need
            order.frozen_cash = need
        else:
            pos.frozen += order.volume
        self._orders[order.id] = order
        self._active[order.id] = order
        self.on_order(order)
        tick = self._ticks.get(order.symbol)
        if tick is not None and self._now is not None and tick.dt.date() == self._now.date() and bar_minute(tick.dt):
            self._match_tick(order, tick)
        return order

    def cancel(self, order_id: str, reason: str = "用户撤单") -> bool:
        order = self._active.pop(order_id, None)
        if order is None:
            return False
        if order.direction == Direction.BUY:
            self.cash += order.frozen_cash
            order.frozen_cash = 0.0
        else:
            pos = self.get_position(order.symbol)
            pos.frozen = max(pos.frozen - order.remaining, 0)
        order.status = OrderStatus.CANCELLED
        order.reason = reason
        order.updated = self._now
        self.on_order(order)
        return True

    def cancel_all(self, reason: str = "全部撤单", symbol: str | None = None) -> int:
        ids = [oid for oid, o in self._active.items() if symbol is None or o.symbol == symbol]
        return sum(self.cancel(oid, reason) for oid in ids)

    # ------------------------------------------------------------- matching
    def on_tick(self, tick: Tick) -> None:
        self.set_time(tick.dt)
        self._ticks[tick.symbol] = tick
        if tick.symbol in self._positions:
            self._positions[tick.symbol].last_price = tick.last
        if bar_minute(tick.dt) is None:
            return
        for order in [o for o in self._active.values() if o.symbol == tick.symbol]:
            self._match_tick(order, tick)

    def _match_tick(self, order: Order, tick: Tick) -> None:
        buy = order.direction == Direction.BUY
        prices = tick.ask_prices if buy else tick.bid_prices
        volumes = tick.ask_volumes if buy else tick.bid_volumes
        has_book = any(p > 0 for p in tick.ask_prices) or any(p > 0 for p in tick.bid_prices)
        if not has_book:
            # 没有盘口（全市场快照模式）：按最新价撮合，但涨停不买、跌停不卖
            if buy and tick.last <= order.price and not (tick.limit_up and tick.last >= tick.limit_up):
                self._fill(order, tick.last, order.remaining, tick.dt)
            elif not buy and tick.last >= order.price and not (tick.limit_down and tick.last <= tick.limit_down):
                self._fill(order, tick.last, order.remaining, tick.dt)
            return
        for px, vol in zip(prices, volumes):
            if order.remaining <= 0:
                break
            if px <= 0 or (buy and px > order.price) or (not buy and px < order.price):
                break
            take = min(order.remaining, int(vol * self.fill_ratio))
            if take < order.remaining:
                take = take // 100 * 100  # 部分成交按整手
            if take <= 0:
                continue
            self._fill(order, px, take, tick.dt)

    def match_bar(self, bar: Bar) -> None:
        """日线撮合（回测用）。bar.pre_close 用于判断涨跌停一字板。"""
        self.set_time(bar.dt)
        up, down = limit_prices(bar.symbol, bar.pre_close) if bar.pre_close > 0 else (0.0, 0.0)
        for order in [o for o in self._active.values() if o.symbol == bar.symbol]:
            if order.direction == Direction.BUY:
                if up and bar.low >= up:
                    continue  # 一字涨停，买不进
                if bar.open <= order.price:
                    self._fill(order, bar.open, order.remaining, bar.dt)
                elif bar.low <= order.price:
                    self._fill(order, order.price, order.remaining, bar.dt)
            else:
                if down and bar.high <= down:
                    continue  # 一字跌停，卖不出
                if bar.open >= order.price:
                    self._fill(order, bar.open, order.remaining, bar.dt)
                elif bar.high >= order.price:
                    self._fill(order, order.price, order.remaining, bar.dt)
        self.mark_price(bar.symbol, bar.close)

    def mark_price(self, symbol: str, price: float) -> None:
        if symbol in self._positions:
            self._positions[symbol].last_price = price

    def _fill(self, order: Order, price: float, volume: int, dt: datetime) -> None:
        commission, stamp, transfer = self.fees.calc(order.symbol, order.direction, price, volume)
        fee = commission + stamp + transfer
        pos = self.get_position(order.symbol)
        if order.direction == Direction.BUY:
            release = order.frozen_cash * volume / order.remaining
            order.frozen_cash -= release
            cost = price * volume + fee
            self.cash += release - cost
            pos.cost = (pos.cost * pos.volume + cost) / (pos.volume + volume)
            pos.volume += volume
            pos.today_bought += volume
        else:
            self.cash += price * volume - fee
            pos.realized_pnl += (price - pos.cost) * volume - fee
            pos.volume -= volume
            pos.frozen = max(pos.frozen - volume, 0)
            if pos.volume == 0:
                pos.cost = 0.0
        pos.last_price = price
        order.avg_price = (order.avg_price * order.traded + price * volume) / (order.traded + volume)
        order.traded += volume
        order.updated = dt
        if order.remaining == 0:
            order.status = OrderStatus.FILLED
            self._active.pop(order.id, None)
            if order.frozen_cash:
                self.cash += order.frozen_cash  # 退回多冻结的预估费用
                order.frozen_cash = 0.0
        else:
            order.status = OrderStatus.PARTIAL
        trade = Trade(order.id, order.symbol, order.direction, price, volume, dt,
                      commission, stamp, transfer, order.strategy)
        self._trades.append(trade)
        self.on_order(order)
        self.on_trade(trade)
