"""核心数据模型：行情快照、K线、委托、成交、持仓、账户。

所有数量单位统一为「股」（akshare 返回的「手」在数据源层就已乘以 100）。
所有时间均为北京时间（Asia/Shanghai）的 naive datetime，由事件时间驱动，
这样仿真行情（加速时钟）和实盘行情走的是同一套逻辑。
"""
from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"  # 按涨跌停价作为保护价的市价单


class OrderStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def active(self) -> bool:
        return self in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)


def _ser(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Enum):
        return value.value
    return value


class _Serializable:
    def to_dict(self) -> dict[str, Any]:
        return {k: _ser(v) for k, v in asdict(self).items()}  # type: ignore[call-overload]


@dataclass(slots=True)
class Tick(_Serializable):
    symbol: str
    dt: datetime
    last: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_close: float = 0.0
    volume: int = 0          # 当日累计成交量（股）
    amount: float = 0.0      # 当日累计成交额（元）
    limit_up: float = 0.0
    limit_down: float = 0.0
    bid_prices: list[float] = field(default_factory=list)   # 买一 ~ 买五
    bid_volumes: list[int] = field(default_factory=list)
    ask_prices: list[float] = field(default_factory=list)   # 卖一 ~ 卖五
    ask_volumes: list[int] = field(default_factory=list)
    name: str = ""
    source: str = ""

    @property
    def bid1(self) -> float:
        return self.bid_prices[0] if self.bid_prices else 0.0

    @property
    def ask1(self) -> float:
        return self.ask_prices[0] if self.ask_prices else 0.0

    @property
    def change_pct(self) -> float:
        if self.pre_close <= 0:
            return 0.0
        return (self.last / self.pre_close - 1) * 100


@dataclass(slots=True)
class Bar(_Serializable):
    symbol: str
    dt: datetime            # K线起始时间（分钟线）或日期 00:00（日线）
    interval: str           # "1m" / "1d"
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0
    pre_close: float = 0.0  # 日线回测用于计算涨跌停


_order_seq = itertools.count(1)
_trade_seq = itertools.count(1)


def next_order_id() -> str:
    return f"O{next(_order_seq):06d}"


def next_trade_id() -> str:
    return f"T{next(_trade_seq):06d}"


@dataclass(slots=True)
class Order(_Serializable):
    symbol: str
    direction: Direction
    price: float
    volume: int
    order_type: OrderType = OrderType.LIMIT
    strategy: str = "manual"
    id: str = field(default_factory=next_order_id)
    status: OrderStatus = OrderStatus.SUBMITTED
    traded: int = 0
    avg_price: float = 0.0
    created: datetime | None = None
    updated: datetime | None = None
    reason: str = ""         # 拒单/撤单原因
    frozen_cash: float = 0.0  # 买单冻结资金（含预估费用）
    broker_order_id: str = ""  # 实盘券商侧委托号

    @property
    def remaining(self) -> int:
        return self.volume - self.traded


@dataclass(slots=True)
class Trade(_Serializable):
    order_id: str
    symbol: str
    direction: Direction
    price: float
    volume: int
    dt: datetime
    commission: float = 0.0
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0
    strategy: str = "manual"
    id: str = field(default_factory=next_trade_id)

    @property
    def fee(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee


@dataclass(slots=True)
class Position(_Serializable):
    symbol: str
    volume: int = 0           # 总持仓
    today_bought: int = 0     # 今日买入（T+1 不可卖）
    frozen: int = 0           # 已挂卖单冻结
    cost: float = 0.0         # 持仓均价（含买入费用）
    last_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def available(self) -> int:
        """可卖数量 = 总持仓 - 今日买入 - 卖单冻结。"""
        return max(self.volume - self.today_bought - self.frozen, 0)

    @property
    def market_value(self) -> float:
        return self.volume * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.cost) * self.volume

    def to_dict(self) -> dict[str, Any]:
        d = {k: _ser(v) for k, v in asdict(self).items()}
        d.update(
            available=self.available,
            market_value=round(self.market_value, 2),
            unrealized_pnl=round(self.unrealized_pnl, 2),
        )
        return d


@dataclass(slots=True)
class AccountSnapshot(_Serializable):
    dt: datetime | None
    cash: float
    frozen_cash: float
    market_value: float
    equity: float
    initial_cash: float
    day_start_equity: float

    @property
    def day_pnl(self) -> float:
        return self.equity - self.day_start_equity

    def to_dict(self) -> dict[str, Any]:
        d = {k: _ser(v) for k, v in asdict(self).items()}
        d.update(
            day_pnl=round(self.day_pnl, 2),
            total_return_pct=round((self.equity / self.initial_cash - 1) * 100, 4)
            if self.initial_cash
            else 0.0,
        )
        return d
