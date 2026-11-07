from .base import Strategy, StrategyContext
from .series import BarSeries

# 内置策略的简称，配置文件和 CLI 里可以直接用简称
BUILTIN = {
    "dual_ma": "quant.strategy.examples.dual_ma.DualMaStrategy",
    "breakout": "quant.strategy.examples.breakout.BreakoutStrategy",
}


def load_strategy_class(path: str) -> type[Strategy]:
    import importlib

    path = BUILTIN.get(path, path)
    module, _, cls = path.rpartition(".")
    obj = getattr(importlib.import_module(module), cls)
    if not (isinstance(obj, type) and issubclass(obj, Strategy)):
        raise TypeError(f"{path} 不是 Strategy 子类")
    return obj


__all__ = ["Strategy", "StrategyContext", "BarSeries", "BUILTIN", "load_strategy_class"]
