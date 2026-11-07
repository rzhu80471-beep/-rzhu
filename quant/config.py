from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "feed": {
        "type": "sim",
        "symbols": ["600000", "000001", "300750", "688981"],
        "names": {},
        "akshare": {"mode": "quote", "interval": 3.0, "max_concurrency": 4, "only_trading_hours": True},
        "sim": {"speed": 30, "tick_interval": 0.5, "seed": None},
        "qmt": {"period": "tick"},
    },
    "broker": {
        "type": "paper",
        "initial_cash": 1_000_000,
        "fill_ratio": 1.0,
        "fees": {"commission_rate": 0.00025, "min_commission": 5.0, "stamp_duty": 0.0005, "transfer_fee": 0.00001},
        "qmt": {"path": "", "account_id": "", "session_id": 0},
    },
    "risk": {},
    "strategies": [],
    "storage": {"path": "data/quant.duckdb", "store_ticks": True},
    "web": {"host": "127.0.0.1", "port": 8000},
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg = DEFAULTS
    if path and Path(path).exists():
        cfg = _merge(DEFAULTS, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    # 代码统一为 6 位字符串（YAML 会把 000001 读成整数 1）
    cfg["feed"]["symbols"] = [str(s).zfill(6) for s in cfg["feed"]["symbols"]]
    for st in cfg.get("strategies") or []:
        st["symbols"] = [str(s).zfill(6) for s in st.get("symbols", [])]
    return cfg
