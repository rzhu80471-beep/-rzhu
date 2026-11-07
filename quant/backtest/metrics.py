from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd

from ..core.models import Direction, Trade


def trade_stats(trades: list[Trade]) -> dict[str, float]:
    """按移动平均成本法逐笔计算卖出盈亏，统计胜率 / 盈亏比。"""
    qty: dict[str, int] = defaultdict(int)
    cost: dict[str, float] = defaultdict(float)
    pnls: list[float] = []
    for t in sorted(trades, key=lambda x: x.dt):
        if t.direction == Direction.BUY:
            total = cost[t.symbol] * qty[t.symbol] + t.price * t.volume + t.fee
            qty[t.symbol] += t.volume
            cost[t.symbol] = total / qty[t.symbol]
        else:
            pnls.append((t.price - cost[t.symbol]) * t.volume - t.fee)
            qty[t.symbol] -= t.volume
            if qty[t.symbol] <= 0:
                qty[t.symbol], cost[t.symbol] = 0, 0.0
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "round_trips": len(pnls),
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (math.inf if wins else 0.0),
        "avg_win": float(np.mean(wins)) if wins else 0.0,
        "avg_loss": float(np.mean(losses)) if losses else 0.0,
        "total_fees": round(sum(t.fee for t in trades), 2),
    }


def performance(equity: pd.Series, trades: list[Trade], periods: int = 252) -> dict[str, float]:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    ret = equity.pct_change().dropna()
    total = equity.iloc[-1] / equity.iloc[0] - 1
    years = len(ret) / periods
    annual = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else 0.0
    dd = equity / equity.cummax() - 1
    max_dd = float(dd.min())
    vol = float(ret.std() * math.sqrt(periods))
    sharpe = float(ret.mean() / ret.std() * math.sqrt(periods)) if ret.std() > 0 else 0.0
    downside = ret[ret < 0].std()
    sortino = float(ret.mean() / downside * math.sqrt(periods)) if downside and downside > 0 else 0.0
    out = {
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "days": len(equity),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return": float(total),
        "annual_return": float(annual),
        "max_drawdown": max_dd,
        "volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": float(annual / -max_dd) if max_dd < 0 else 0.0,
        "trades": len(trades),
    }
    out.update(trade_stats(trades))
    return out
