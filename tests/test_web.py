from datetime import datetime

from fastapi.testclient import TestClient

from quant.config import load_config
from quant.data.sim_feed import SimFeed
from quant.data.store import Store
from quant.engine.live import LiveEngine
from quant.web.server import create_app


def make_client():
    cfg = load_config(None)
    cfg["storage"]["path"] = ":memory:"
    eng = LiveEngine(cfg, store=Store(":memory:"))
    eng.feed = SimFeed(eng.bus, ["600000", "000001"], speed=60, tick_interval=0.05, seed=1,
                       start=datetime(2026, 11, 9, 9, 30, 0))
    return eng, TestClient(create_app(eng))


def test_rest_and_websocket():
    eng, client = make_client()
    with client:
        for _ in range(5):
            eng.feed.step()
        assert client.get("/api/status").json()["feed"]["source"] == "sim"
        assert len(client.get("/api/ticks").json()) == 2
        r = client.post("/api/order", json={"symbol": "600000", "direction": "BUY", "volume": 100, "order_type": "MARKET"}).json()
        assert r["status"] == "FILLED"
        assert client.get("/api/positions").json()[0]["available"] == 0
        assert client.post("/api/order", json={"symbol": "600000", "direction": "BUY", "volume": 100}).status_code == 400
        assert client.post("/api/watchlist", json={"symbol": "600519"}).json()["symbols"][-1] == "600519"
        with client.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot" and snap["data"]["positions"][0]["symbol"] == "600000"
            types = {ws.receive_json()["type"] for _ in range(6)}
            assert types & {"ticks", "account", "position", "bar"}
        ks = client.post("/api/killswitch", json={"on": True}).json()
        assert ks["kill_switch"] is True


def test_backtest_endpoint_with_synthetic():
    _, client = make_client()
    with client:
        r = client.post("/api/backtest", json={"strategy": "dual_ma", "symbols": ["600000"], "start": "2023-01-01",
                                               "end": "2024-12-31", "synthetic": True})
        assert r.status_code == 200
        body = r.json()
        assert body["metrics"]["days"] > 400 and body["equity"][0]["date"].startswith("2023")
        r = client.post("/api/backtest", json={"strategy": "dual_ma", "symbols": ["600000"], "synthetic": False})
        assert r.status_code == 400
