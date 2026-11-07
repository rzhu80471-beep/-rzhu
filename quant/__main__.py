"""命令行入口。

    python -m quant run [-c config.yaml] [--feed sim|akshare|qmt]   启动实时引擎 + Web 看板
    python -m quant backtest dual_ma 600000 000001 --start 2022-01-01 [--synthetic]
    python -m quant download 600000 000001 --start 20200101 [--minute]
    python -m quant probe 600000                                   测试 akshare 实时接口是否可用
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def open_store(path: str, allow_memory: bool = False):
    """DuckDB 同一时间只允许一个进程写库：平台运行时命令行打不开同一个文件。"""
    import duckdb

    from .data.store import Store

    try:
        return Store(path)
    except duckdb.IOException:
        if allow_memory:
            print("[提示] 数据库被正在运行的平台占用，本次使用内存库（只能用合成数据）")
            return Store(":memory:")
        print("[错误] 数据库被正在运行的平台（python -m quant run）占用。\n"
              "       请先停止平台再执行，或直接在网页「策略回测」页里下载数据 / 回测。")
        sys.exit(2)


def cmd_run(args: argparse.Namespace) -> None:
    import uvicorn

    from .config import load_config
    from .engine.live import LiveEngine
    from .web.server import create_app

    cfg = load_config(args.config)
    if args.feed:
        cfg["feed"]["type"] = args.feed
    if args.port:
        cfg["web"]["port"] = args.port
    engine = LiveEngine(cfg)
    app = create_app(engine)
    host, port = cfg["web"]["host"], cfg["web"]["port"]
    print(f"\n  A股量化交易平台已启动：http://{host}:{port}   （行情源 {cfg['feed']['type']}，券商 {cfg['broker']['type']}）\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def cmd_backtest(args: argparse.Namespace) -> None:
    from .backtest.engine import BacktestEngine
    from .config import load_config
    from .core.rules import FeeModel
    from .data.history import make_synthetic_daily
    from .strategy import load_strategy_class

    cfg = load_config(args.config)
    store = open_store(cfg["storage"]["path"], allow_memory=args.synthetic)
    data = {}
    for i, sym in enumerate(args.symbols):
        sym = sym.zfill(6)
        df = store.load_bars(sym, "1d")
        if df.empty and args.synthetic:
            print(f"[提示] {sym} 无本地日线，使用合成数据（不代表真实行情）")
            df = make_synthetic_daily(sym, args.start or "2022-01-01", args.end or "2025-12-31", seed=i)
        if df.empty:
            print(f"[跳过] {sym} 无本地日线，请先运行: python -m quant download {sym}")
            continue
        data[sym] = df
    if not data:
        sys.exit(1)
    params = json.loads(args.params) if args.params else {}
    bt = BacktestEngine(data, args.cash, FeeModel(**cfg["broker"].get("fees", {})))
    res = bt.run(load_strategy_class(args.strategy), list(data), params, start=args.start, end=args.end)
    print(res.summary())
    if args.output:
        res.equity.to_csv(args.output)
        print(f"权益曲线已保存到 {args.output}")


def cmd_download(args: argparse.Namespace) -> None:
    from .config import load_config
    from .data.history import download_daily, download_minute

    cfg = load_config(args.config)
    store = open_store(cfg["storage"]["path"])
    syms = [s.zfill(6) for s in args.symbols]
    res = download_minute(store, syms) if args.minute else download_daily(store, syms, args.start, args.end, args.adjust)
    for s, n in res.items():
        print(f"{s}: {'失败' if n < 0 else f'{n} 条'}")


def cmd_probe(args: argparse.Namespace) -> None:
    """快速检查 akshare 实时接口：网络是否可达、延迟多少、返回是否完整。"""
    import time

    import akshare as ak

    from .core.calendar import now_cn
    from .data.akshare_feed import parse_bid_ask

    for sym in args.symbols:
        t0 = time.monotonic()
        try:
            df = ak.stock_bid_ask_em(symbol=sym)
            tick = parse_bid_ask(sym, dict(zip(df["item"], df["value"])), now_cn())
            ms = (time.monotonic() - t0) * 1000
            if tick:
                print(f"{sym} OK {ms:.0f}ms  最新 {tick.last}  买一 {tick.bid1}/{tick.bid_volumes[0]}  卖一 {tick.ask1}/{tick.ask_volumes[0]}  "
                      f"累计量 {tick.volume}  涨停 {tick.limit_up} 跌停 {tick.limit_down}")
            else:
                print(f"{sym} 无最新价（停牌或未开盘） {ms:.0f}ms")
        except Exception as exc:  # noqa: BLE001
            print(f"{sym} 失败: {exc}")
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m quant", description="A 股量化交易平台")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="启动实时引擎和 Web 看板")
    r.add_argument("--feed", choices=["sim", "akshare", "qmt"])
    r.add_argument("--port", type=int)
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("backtest", help="日线回测")
    b.add_argument("strategy", help="内置简称（dual_ma / breakout）或完整类路径")
    b.add_argument("symbols", nargs="+")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--params", help='JSON，例如 \'{"fast": 5, "slow": 20}\'')
    b.add_argument("--cash", type=float, default=1_000_000)
    b.add_argument("--synthetic", action="store_true", help="本地无数据时使用合成日线")
    b.add_argument("-o", "--output", help="把权益曲线保存为 CSV")
    b.set_defaults(func=cmd_backtest)

    d = sub.add_parser("download", help="用 akshare 下载历史数据入库")
    d.add_argument("symbols", nargs="+")
    d.add_argument("--start", default="20200101")
    d.add_argument("--end")
    d.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", ""])
    d.add_argument("--minute", action="store_true", help="下载近几个交易日的 1 分钟线")
    d.set_defaults(func=cmd_download)

    pr = sub.add_parser("probe", help="测试 akshare 实时行情接口")
    pr.add_argument("symbols", nargs="*", default=["600000", "000001"])
    pr.set_defaults(func=cmd_probe)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
