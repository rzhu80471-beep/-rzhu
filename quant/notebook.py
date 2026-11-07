"""在 Jupyter Notebook 里使用平台。

Jupyter 内核自己已经在运行一个 asyncio 事件循环，直接调用 `uvicorn.run` / `asyncio.run` 会报
"cannot be called from a running event loop"。这里把 Web 服务放进后台线程（线程里有自己的事件循环），
notebook 不会被阻塞，可以一边开着看板一边写代码做研究。

    from quant.notebook import start_platform, stop_platform, backtest, plot_backtest

    engine = start_platform()                 # 仿真行情；start_platform(feed="akshare") 接东方财富
    res = backtest("dual_ma", ["600000", "000001"], start="2022-01-01", synthetic=True)
    plot_backtest(res)
    stop_platform()
"""
from __future__ import annotations

import logging
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from .backtest.engine import BacktestEngine, BacktestResult
from .config import load_config
from .core.rules import FeeModel
from .data.store import Store

ROOT = Path(__file__).resolve().parents[1]
_state: dict[str, Any] = {"server": None, "thread": None, "engine": None, "url": None}


def _resolve_cfg(config: str | Path | None) -> dict[str, Any]:
    path = Path(config) if config else ROOT / "config.yaml"
    if not path.is_absolute() and not path.exists():
        path = ROOT / path
    cfg = load_config(path)
    db = Path(cfg["storage"]["path"])
    if str(db) != ":memory:" and not db.is_absolute():
        cfg["storage"]["path"] = str(ROOT / db)   # 不管 notebook 在哪个目录打开，都用项目里的数据库
    return cfg


def start_platform(config: str | Path | None = None, feed: str | None = None, port: int | None = None,
                   open_browser: bool = True, timeout: float = 30.0):
    """在后台线程启动实时引擎 + Web 看板，返回 LiveEngine（可在 notebook 里直接查询状态）。"""
    if _state["server"] is not None and _state["thread"].is_alive():
        print(f"平台已经在运行：{_state['url']}")
        return _state["engine"]

    import uvicorn

    from .engine.live import LiveEngine
    from .web.server import create_app

    logging.basicConfig(level=logging.WARNING)
    cfg = _resolve_cfg(config)
    if feed:
        cfg["feed"]["type"] = feed
    if port:
        cfg["web"]["port"] = port
    engine = LiveEngine(cfg)
    host, port = cfg["web"]["host"], cfg["web"]["port"]
    server = uvicorn.Server(uvicorn.Config(create_app(engine), host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="quant-web", daemon=True)
    thread.start()
    deadline = time.time() + timeout
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:
        engine.store.close()
        raise RuntimeError(f"平台启动失败（端口 {port} 可能被占用，可以试试 start_platform(port=8080)）")
    url = f"http://{host}:{port}"
    _state.update(server=server, thread=thread, engine=engine, url=url)
    print(f"A股量化交易平台已启动：{url}  （行情源 {cfg['feed']['type']}，券商 {cfg['broker']['type']}）")
    if open_browser:
        webbrowser.open(url)
    return engine


def stop_platform(timeout: float = 15.0) -> None:
    server, thread = _state["server"], _state["thread"]
    if server is None:
        print("平台没有在运行")
        return
    server.should_exit = True
    thread.join(timeout)
    if _state["engine"] is not None:
        _state["engine"].store.close()
    _state.update(server=None, thread=None, engine=None, url=None)
    print("平台已停止")


def _store() -> tuple[Store, bool]:
    """平台运行时复用它的数据库连接（DuckDB 同一文件只能被一个进程打开），否则临时打开。"""
    if _state["engine"] is not None:
        return _state["engine"].store, False
    return Store(_resolve_cfg(None)["storage"]["path"]), True


def download(symbols: list[str], start: str = "20200101", end: str | None = None, adjust: str = "qfq") -> dict[str, int]:
    """用 akshare 下载日线入库（需中国大陆网络）。"""
    from .data.history import download_daily

    store, owned = _store()
    try:
        return download_daily(store, [s.zfill(6) for s in symbols], start, end, adjust)
    finally:
        if owned:
            store.close()


def load_bars(symbol: str, interval: str = "1d", start: str | None = None, end: str | None = None):
    """从本地数据库读取 K 线（pandas DataFrame），方便在 notebook 里做研究。"""
    store, owned = _store()
    try:
        return store.load_bars(symbol.zfill(6), interval, start, end)
    finally:
        if owned:
            store.close()


def backtest(strategy: str | type, symbols: list[str], start: str | None = None, end: str | None = None,
             params: dict[str, Any] | None = None, initial_cash: float = 1_000_000,
             synthetic: bool = False) -> BacktestResult:
    """日线回测。strategy 可以是内置简称（dual_ma / breakout）、类路径，或 notebook 里定义的 Strategy 子类。"""
    from .data.history import make_synthetic_daily
    from .strategy import load_strategy_class

    cls = load_strategy_class(strategy) if isinstance(strategy, str) else strategy
    store, owned = _store()
    try:
        data = {}
        for i, sym in enumerate(s.zfill(6) for s in symbols):
            df = store.load_bars(sym, "1d")
            if df.empty and synthetic:
                print(f"[提示] {sym} 无本地日线，使用合成数据（不代表真实行情）")
                df = make_synthetic_daily(sym, start or "2022-01-01", end or "2025-12-31", seed=i)
            if df.empty:
                print(f"[跳过] {sym} 无本地日线，先运行 download(['{sym}'])，或传 synthetic=True")
                continue
            data[sym] = df
    finally:
        if owned:
            store.close()
    if not data:
        raise ValueError("没有可用于回测的数据")
    fees = FeeModel(**_resolve_cfg(None)["broker"].get("fees", {}))
    res = BacktestEngine(data, initial_cash, fees).run(cls, list(data), params, start=start, end=end)
    print(res.summary())
    return res


def plot_backtest(res: BacktestResult, title: str = "回测结果"):
    """权益曲线（策略 vs 等权持有基准）+ 回撤，两张图上下排列、共用时间轴（不用双 Y 轴）。"""
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 中文字体：Windows 微软雅黑 / macOS 苹方 / Linux Noto
    for name in ("Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimHei", "WenQuanYi Micro Hei"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False

    eq = res.equity
    blue, orange, ink, muted, grid = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#e2e1dc"
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(eq.index, eq["equity"] / 1e4, color=blue, lw=2, label="策略权益")
    ax1.plot(eq.index, eq["benchmark"] / 1e4, color=orange, lw=2, label="等权持有基准")
    for col, color in (("equity", blue), ("benchmark", orange)):
        ax1.annotate(f"{eq[col].iloc[-1] / 1e4:,.1f} 万", (eq.index[-1], eq[col].iloc[-1] / 1e4),
                     xytext=(6, 0), textcoords="offset points", va="center", color=ink, fontsize=9)
    ax1.set_ylabel("权益（万元）", color=muted)
    ax1.legend(loc="upper left", frameon=False)
    m = res.metrics
    ax1.set_title(f"{title}　总收益 {m['total_return']:.1%}　年化 {m['annual_return']:.1%}　"
                  f"最大回撤 {m['max_drawdown']:.1%}　夏普 {m['sharpe']:.2f}", loc="left", fontsize=11, color=ink)
    ax2.fill_between(eq.index, eq["drawdown"] * 100, 0, color=blue, alpha=0.25, lw=0)
    ax2.plot(eq.index, eq["drawdown"] * 100, color=blue, lw=1.5)
    ax2.set_ylabel("回撤（%）", color=muted)
    for ax in (ax1, ax2):
        ax.grid(color=grid, lw=0.8)
        ax.tick_params(colors=muted)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(grid)
    fig.tight_layout()
    return fig
