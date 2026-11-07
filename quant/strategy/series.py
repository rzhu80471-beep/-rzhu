"""K 线序列 + 常用指标（类似 vnpy 的 ArrayManager），用 numpy 定长缓存，增量更新。"""
from __future__ import annotations

import numpy as np

from ..core.models import Bar


class BarSeries:
    def __init__(self, size: int = 200):
        self.size = size
        self.count = 0
        self.open = np.zeros(size)
        self.high = np.zeros(size)
        self.low = np.zeros(size)
        self.close = np.zeros(size)
        self.volume = np.zeros(size)

    @property
    def ready(self) -> bool:
        return self.count >= self.size

    def update(self, bar: Bar) -> None:
        self.count += 1
        for arr, val in ((self.open, bar.open), (self.high, bar.high), (self.low, bar.low),
                         (self.close, bar.close), (self.volume, bar.volume)):
            arr[:-1] = arr[1:]
            arr[-1] = val

    def has(self, n: int) -> bool:
        return self.count >= n

    # --- 指标：返回最新值；array=True 返回整段
    def sma(self, n: int, array: bool = False):
        c = self.close
        out = np.convolve(c, np.ones(n) / n, mode="valid")
        return out if array else float(out[-1])

    def ema(self, n: int, array: bool = False):
        alpha = 2 / (n + 1)
        out = np.empty_like(self.close)
        out[0] = self.close[0]
        for i in range(1, len(out)):
            out[i] = alpha * self.close[i] + (1 - alpha) * out[i - 1]
        return out if array else float(out[-1])

    def std(self, n: int) -> float:
        return float(np.std(self.close[-n:], ddof=0))

    def boll(self, n: int = 20, k: float = 2.0) -> tuple[float, float, float]:
        mid = self.sma(n)
        sd = self.std(n)
        return mid + k * sd, mid, mid - k * sd

    def rsi(self, n: int = 14) -> float:
        diff = np.diff(self.close[-(n + 1):])
        gain = diff[diff > 0].sum() / n
        loss = -diff[diff < 0].sum() / n
        if loss == 0:
            return 100.0
        return float(100 - 100 / (1 + gain / loss))

    def atr(self, n: int = 14) -> float:
        h, lo, c = self.high[-n:], self.low[-n:], self.close[-(n + 1):-1]
        tr = np.maximum(h - lo, np.maximum(np.abs(h - c), np.abs(lo - c)))
        return float(tr.mean())

    def highest(self, n: int, exclude_last: bool = True) -> float:
        arr = self.high[-(n + 1):-1] if exclude_last else self.high[-n:]
        return float(arr.max())

    def lowest(self, n: int, exclude_last: bool = True) -> float:
        arr = self.low[-(n + 1):-1] if exclude_last else self.low[-n:]
        return float(arr.min())
