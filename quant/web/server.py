"""Web 服务：REST API + WebSocket 实时推送 + 静态前端。

WebSocket 推送做了两件事保证不卡：
- 每个客户端一个有上限的异步队列（满了丢最旧），慢客户端不影响行情链路
- Tick 按股票合并，每 250ms 只推最新一笔；全市场模式下也不会把浏览器打爆
"""
from __future__ import annotations

import asyncio
import calendar
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..backtest.engine import BacktestEngine
from ..core.events import ACCOUNT, BAR, LOG, ORDER, POSITION, STATUS, TICK, TRADE
from ..core.models import Bar, Direction, OrderType
from ..core.rules import FeeModel, board_of
from ..data.history import download_daily, make_synthetic_daily
from ..engine.live import LiveEngine
from ..strategy import BUILTIN, load_strategy_class

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
TICK_FLUSH = 0.25


def ts(dt: datetime) -> int:
    """lightweight-charts 按 UTC 显示时间，这里把北京时间直接当 UTC 传，坐标轴即显示北京时间。"""
    return calendar.timegm(dt.timetuple())


def bar_json(b: Bar) -> dict[str, Any]:
    return {"time": ts(b.dt), "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}


def _jsonable(topic: str, data: Any) -> Any:
    if topic == POSITION:
        return [p.to_dict() for p in data]
    if topic == BAR:
        return {**data.to_dict(), "time": ts(data.dt)}
    if hasattr(data, "to_dict"):
        return data.to_dict()
    return data


class OrderReq(BaseModel):
    symbol: str
    direction: Direction
    volume: int = Field(gt=0)
    price: float | None = None
    order_type: OrderType = OrderType.LIMIT


class BacktestReq(BaseModel):
    strategy: str = "dual_ma"
    symbols: list[str] = Field(default_factory=lambda: ["600000"])
    start: str | None = None
    end: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    initial_cash: float = 1_000_000
    synthetic: bool = False   # 数据库里没有日线时用合成数据（仅用于联调）


class DownloadReq(BaseModel):
    symbols: list[str]
    start: str = "20200101"
    end: str | None = None
    adjust: str = "qfq"


def create_app(engine: LiveEngine, autostart: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autostart:
            await engine.start()
        yield
        if autostart:
            await engine.stop()

    app = FastAPI(title="A股量化交易平台", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------------ 查询
    @app.get("/api/status")
    async def status():
        return engine.status()

    @app.get("/api/ticks")
    async def ticks():
        return [t.to_dict() for t in engine.ticks.values()]

    @app.get("/api/symbols")
    async def symbols():
        return [{"symbol": s, "name": engine.names.get(s, ""), "board": board_of(s).value} for s in engine.feed.symbols]

    @app.get("/api/bars")
    async def bars(symbol: str, interval: str = "1m", limit: int = 600):
        mem = list(engine.bars.get((symbol, interval), []))
        out: list[dict] = []
        if len(mem) < limit:
            df = await asyncio.to_thread(engine.store.load_bars, symbol, interval, None, None, limit)
            first_mem = mem[0].dt if mem else None
            for r in df.itertuples():
                dt = pd.Timestamp(r.dt).to_pydatetime()
                if first_mem is None or dt < first_mem:
                    out.append({"time": ts(dt), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": int(r.volume)})
        out += [bar_json(b) for b in mem]
        if interval == "1m" and (cur := engine.bar_gen.current(symbol)):
            out.append(bar_json(cur))   # 正在形成中的 K 线
        dedup = {b["time"]: b for b in out}
        return [dedup[k] for k in sorted(dedup)][-limit:]

    @app.get("/api/account")
    async def account():
        return engine.broker.account().to_dict()

    @app.get("/api/positions")
    async def positions():
        return [p.to_dict() for p in engine.broker.positions()]

    @app.get("/api/orders")
    async def orders(active: bool = False):
        return [o.to_dict() for o in engine.broker.orders(active)][::-1]

    @app.get("/api/trades")
    async def trades():
        return [t.to_dict() for t in engine.broker.trades()][::-1]

    @app.get("/api/strategies")
    async def strategies():
        return {"running": [s.info() for s in engine.strategies], "builtin": list(BUILTIN)}

    # ------------------------------------------------------------------ 操作
    @app.post("/api/order")
    async def place_order(req: OrderReq):
        if req.order_type == OrderType.LIMIT and not req.price:
            raise HTTPException(400, "限价单需要价格")
        order = engine.send_order(None, req.symbol.zfill(6), req.direction, req.volume, req.price, req.order_type)
        return order.to_dict()

    @app.post("/api/order/{order_id}/cancel")
    async def cancel(order_id: str):
        return {"ok": engine.cancel_order(order_id)}

    @app.post("/api/cancel_all")
    async def cancel_all():
        n = sum(engine.cancel_order(o.id) for o in engine.broker.orders(active_only=True))
        return {"cancelled": n}

    @app.post("/api/killswitch")
    async def killswitch(body: dict):
        on = bool(body.get("on"))
        engine.risk.set_kill_switch(on, "手动熔断")
        if on:
            for o in engine.broker.orders(active_only=True):
                engine.broker.cancel(o.id, "风控熔断撤单")
        engine.write_log(f"手动{'开启' if on else '解除'}熔断", "risk", "warning")
        engine.bus.publish(STATUS, {"risk": engine.risk.status()})
        return engine.risk.status()

    @app.post("/api/watchlist")
    async def watchlist(body: dict):
        sym = str(body.get("symbol", "")).strip().zfill(6)
        if not sym.isdigit() or len(sym) != 6:
            raise HTTPException(400, "股票代码应为 6 位数字")
        if body.get("action") == "remove":
            engine.feed.remove_symbol(sym)
        else:
            engine.feed.add_symbol(sym)
        return {"symbols": engine.feed.symbols}

    @app.post("/api/backtest")
    async def backtest(req: BacktestReq):
        try:
            cls = load_strategy_class(req.strategy)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"加载策略失败: {exc}") from exc
        syms = [s.zfill(6) for s in req.symbols]

        def run():
            data = {}
            for i, s in enumerate(syms):
                df = engine.store.load_bars(s, "1d")
                if df.empty and req.synthetic:
                    df = make_synthetic_daily(s, req.start or "2022-01-01", req.end or "2025-12-31", seed=i)
                if not df.empty:
                    data[s] = df
            if not data:
                raise HTTPException(400, "数据库中没有这些股票的日线，请先下载历史数据，或勾选「使用合成数据」")
            bt = BacktestEngine(data, req.initial_cash, FeeModel(**engine.cfg["broker"].get("fees", {})))
            return bt.run(cls, list(data), req.params, start=req.start, end=req.end)

        try:
            res = await asyncio.to_thread(run)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("回测失败")
            raise HTTPException(400, f"回测失败: {exc}") from exc
        out = res.to_dict()
        out["summary"] = res.summary()
        return JSONResponse(out)

    @app.post("/api/history/download")
    async def history_download(req: DownloadReq):
        res = await asyncio.to_thread(download_daily, engine.store, [s.zfill(6) for s in req.symbols], req.start, req.end, req.adjust)
        return {"result": res}

    @app.get("/api/history/available")
    async def history_available(interval: str = "1d"):
        df = await asyncio.to_thread(engine.store.available, interval)
        return df.astype(str).to_dict("records")

    # ------------------------------------------------------------------ WebSocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        sub = engine.bus.stream([TICK, BAR, ORDER, TRADE, ACCOUNT, POSITION, LOG, STATUS], maxsize=5000)
        try:
            await websocket.send_json({"type": "snapshot", "data": {
                "status": engine.status(),
                "ticks": [t.to_dict() for t in engine.ticks.values()],
                "account": engine.broker.account().to_dict(),
                "positions": [p.to_dict() for p in engine.broker.positions()],
                "orders": [o.to_dict() for o in engine.broker.orders()][-200:][::-1],
                "trades": [t.to_dict() for t in engine.broker.trades()][-200:][::-1],
                "logs": list(engine.logs)[-100:],
                "names": engine.names,
            }})
            pending_ticks: dict[str, dict] = {}
            loop = asyncio.get_running_loop()
            next_flush = loop.time() + TICK_FLUSH
            while True:
                timeout = max(next_flush - loop.time(), 0)
                try:
                    ev = await asyncio.wait_for(sub.get(), timeout=timeout)
                    if ev.topic == TICK:
                        pending_ticks[ev.data.symbol] = ev.data.to_dict()
                    elif ev.topic in (ACCOUNT, POSITION, STATUS):
                        msg = {"type": ev.topic, "data": _jsonable(ev.topic, ev.data)}
                        if ev.topic == STATUS:
                            msg["data"] = {**(msg["data"] or {}), "engine": engine.status()}
                        await websocket.send_json(msg)
                    else:
                        await websocket.send_json({"type": ev.topic, "data": _jsonable(ev.topic, ev.data)})
                except asyncio.TimeoutError:
                    pass
                if loop.time() >= next_flush:
                    if pending_ticks:
                        await websocket.send_json({"type": "ticks", "data": list(pending_ticks.values()),
                                                   "now": engine.now().isoformat(timespec="seconds")})
                        pending_ticks = {}
                    next_flush = loop.time() + TICK_FLUSH
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("WebSocket 异常")
        finally:
            sub.close()

    return app
