"""事前风控。所有委托（策略和手动）都必须经过这里。

- 熔断开关：手动一键停止，或当日亏损超限自动触发
- 单笔金额上限、单票持仓占比上限、黑名单、可选禁止买入 ST
- 报单频率：每秒 / 每日笔数上限。2025 年起施行的《证券市场程序化交易管理规定》要求程序化交易报告，
  并把「单账户每秒申报、撤单最高 300 笔以上或单日最高 20000 笔以上」认定为高频交易，
  默认阈值远低于此，可按需调整
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from ..core.models import AccountSnapshot, Direction, Order, OrderType
from ..core.rules import is_st


@dataclass
class RiskConfig:
    enabled: bool = True
    max_order_value: float = 500_000
    max_position_pct: float = 0.30
    max_daily_loss_pct: float = 0.05
    max_orders_per_second: int | None = 5
    max_orders_per_day: int | None = 2000
    max_cancels_per_day: int | None = 1000
    block_st_buy: bool = True
    blacklist: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: RiskConfig | None = None):
        self.cfg = cfg or RiskConfig()
        self.kill_switch = False
        self.kill_reason = ""
        self._recent: deque[float] = deque()
        self._day: date | None = None
        self.orders_today = 0
        self.cancels_today = 0
        self.rejected = 0

    def _roll(self, d: date | None) -> None:
        if d and d != self._day:
            self._day = d
            self.orders_today = 0
            self.cancels_today = 0

    def set_kill_switch(self, on: bool, reason: str = "手动") -> None:
        self.kill_switch = on
        self.kill_reason = reason if on else ""

    def check(self, order: Order, account: AccountSnapshot, position_value: float, name: str = "",
              ref_price: float = 0.0) -> str | None:
        """返回 None 表示通过，否则返回拒单原因。position_value 为该股票当前持仓市值。"""
        if not self.cfg.enabled:
            return None
        self._roll(account.dt.date() if account.dt else None)
        if self.kill_switch:
            return f"风控熔断中（{self.kill_reason}），禁止新开委托"
        if order.symbol in self.cfg.blacklist:
            return "股票在黑名单中"
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 1.0:
            self._recent.popleft()
        if self.cfg.max_orders_per_second and len(self._recent) >= self.cfg.max_orders_per_second:
            return f"报单过快（每秒上限 {self.cfg.max_orders_per_second} 笔）"
        if self.cfg.max_orders_per_day and self.orders_today >= self.cfg.max_orders_per_day:
            return f"超过当日报单上限 {self.cfg.max_orders_per_day} 笔"
        if order.direction == Direction.BUY:
            if self.cfg.block_st_buy and is_st(name):
                return "风控禁止买入 ST 股票"
            price = order.price if order.order_type == OrderType.LIMIT and order.price > 0 else ref_price
            value = price * order.volume
            if value > self.cfg.max_order_value:
                return f"单笔金额 {value:,.0f} 超过上限 {self.cfg.max_order_value:,.0f}"
            if account.equity > 0 and (position_value + value) / account.equity > self.cfg.max_position_pct:
                return f"买入后单票仓位将超过 {self.cfg.max_position_pct:.0%}"
        self._recent.append(now)
        self.orders_today += 1
        return None

    def check_cancel(self) -> str | None:
        if self.cfg.enabled and self.cfg.max_cancels_per_day and self.cancels_today >= self.cfg.max_cancels_per_day:
            return f"超过当日撤单上限 {self.cfg.max_cancels_per_day} 笔"
        self.cancels_today += 1
        return None

    def on_account(self, account: AccountSnapshot) -> str | None:
        """账户更新时检查当日亏损，触发熔断时返回原因。"""
        if not self.cfg.enabled or self.kill_switch or account.day_start_equity <= 0:
            return None
        loss = -account.day_pnl / account.day_start_equity
        if loss >= self.cfg.max_daily_loss_pct:
            self.set_kill_switch(True, f"当日亏损 {loss:.2%} 超过 {self.cfg.max_daily_loss_pct:.0%}")
            return self.kill_reason
        return None

    def status(self) -> dict:
        return {"enabled": self.cfg.enabled, "kill_switch": self.kill_switch, "kill_reason": self.kill_reason,
                "orders_today": self.orders_today, "cancels_today": self.cancels_today, "rejected": self.rejected}
