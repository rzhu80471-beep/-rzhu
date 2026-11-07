"""A 股交易规则：板块识别、涨跌停、最小交易单位、交易费用。

规则以 2025 年沪深北交易所现行规则为准，变化时只需改这里：
- 主板涨跌幅 10%，主板 ST 5%；创业板、科创板 20%；北交所 30%
- 主板 / 创业板买入须为 100 股整数倍；科创板单笔不少于 200 股、以 1 股递增；
  北交所单笔不少于 100 股、以 1 股递增；卖出时不足一个最小单位的余股须一次卖出
- 印花税 0.05%（仅卖出）；过户费 0.001%（双向）；佣金按券商约定（默认万 2.5，最低 5 元）
- 注册制新股上市前 5 个交易日不设涨跌幅 —— 这里未建模，回测时请自行剔除新股
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from .models import Direction


class Board(str, Enum):
    SH_MAIN = "沪主板"
    SZ_MAIN = "深主板"
    CHINEXT = "创业板"
    STAR = "科创板"
    BSE = "北交所"
    OTHER = "其他"


def board_of(symbol: str) -> Board:
    s = symbol.strip()
    if s.startswith(("688", "689")):
        return Board.STAR
    if s.startswith(("600", "601", "603", "605")):
        return Board.SH_MAIN
    if s.startswith(("300", "301", "302")):
        return Board.CHINEXT
    if s.startswith(("000", "001", "002", "003")):
        return Board.SZ_MAIN
    if s.startswith(("920", "43", "83", "87", "88")):
        return Board.BSE
    return Board.OTHER


def exchange_of(symbol: str) -> str:
    """返回 SH / SZ / BJ。"""
    b = board_of(symbol)
    if b == Board.BSE:
        return "BJ"
    if b in (Board.SH_MAIN, Board.STAR) or symbol.startswith(("5", "9")):
        return "SH"  # 5 开头为沪市基金/ETF，900 为沪市 B 股
    return "SZ"


def is_st(name: str) -> bool:
    return "ST" in name.upper()


def limit_pct(symbol: str, name: str = "") -> float:
    b = board_of(symbol)
    if b in (Board.CHINEXT, Board.STAR):
        return 0.20
    if b == Board.BSE:
        return 0.30
    if is_st(name):
        return 0.05
    return 0.10


def round_price(price: float) -> float:
    """按 0.01 元最小价位四舍五入。"""
    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_prices(symbol: str, pre_close: float, name: str = "") -> tuple[float, float]:
    """返回 (涨停价, 跌停价)。"""
    if pre_close <= 0:
        return 0.0, 0.0
    pct = limit_pct(symbol, name)
    return round_price(pre_close * (1 + pct)), round_price(pre_close * (1 - pct))


def min_buy_volume(symbol: str) -> int:
    return 200 if board_of(symbol) == Board.STAR else 100


def round_buy_volume(symbol: str, volume: float) -> int:
    """把目标买入数量向下取整到合法数量，不足最小单位返回 0。"""
    v = int(volume)
    b = board_of(symbol)
    if b in (Board.STAR, Board.BSE):
        return v if v >= min_buy_volume(symbol) else 0
    return v // 100 * 100


def check_volume(symbol: str, direction: Direction, volume: int, available: int = 0) -> str | None:
    """校验委托数量是否合法，合法返回 None，否则返回原因。"""
    if volume <= 0:
        return "委托数量必须大于 0"
    b = board_of(symbol)
    if direction == Direction.BUY:
        if b in (Board.STAR, Board.BSE):
            if volume < min_buy_volume(symbol):
                return f"{b.value}单笔买入不少于 {min_buy_volume(symbol)} 股"
        elif volume % 100:
            return "买入数量须为 100 股的整数倍"
        return None
    # 卖出
    if volume > available:
        return f"可卖数量不足（可卖 {available} 股）"
    if volume == available:
        return None  # 清仓允许零股
    if b in (Board.STAR, Board.BSE):
        if volume < min_buy_volume(symbol):
            return f"{b.value}单笔卖出不少于 {min_buy_volume(symbol)} 股（清仓除外）"
    elif volume % 100:
        return "卖出数量须为 100 股整数倍（清仓零股除外）"
    return None


@dataclass(slots=True)
class FeeModel:
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty: float = 0.0005      # 仅卖出
    transfer_fee: float = 0.00001   # 沪深双向；北交所无过户费

    def calc(self, symbol: str, direction: Direction, price: float, volume: int) -> tuple[float, float, float]:
        """返回 (佣金, 印花税, 过户费)。"""
        value = price * volume
        commission = max(value * self.commission_rate, self.min_commission) if value > 0 else 0.0
        stamp = value * self.stamp_duty if direction == Direction.SELL else 0.0
        transfer = 0.0 if board_of(symbol) == Board.BSE else value * self.transfer_fee
        return round(commission, 2), round(stamp, 2), round(transfer, 2)

    def total(self, symbol: str, direction: Direction, price: float, volume: int) -> float:
        return sum(self.calc(symbol, direction, price, volume))
