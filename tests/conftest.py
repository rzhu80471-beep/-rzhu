import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.core.models import Tick  # noqa: E402


def make_tick(symbol="600000", dt=datetime(2026, 11, 9, 10, 0, 0), last=10.0, volume=0, pre_close=10.0,
              bids=None, asks=None, bid_vols=None, ask_vols=None, up=11.0, down=9.0, amount=None, name=""):
    bids = bids if bids is not None else [round(last - 0.01 * i, 2) for i in range(5)]
    asks = asks if asks is not None else [round(last + 0.01 * (i + 1), 2) for i in range(5)]
    return Tick(symbol=symbol, dt=dt, last=last, open=pre_close, high=last, low=last, pre_close=pre_close,
                volume=volume, amount=amount if amount is not None else volume * last, limit_up=up, limit_down=down,
                bid_prices=bids, bid_volumes=bid_vols or [10000] * len(bids),
                ask_prices=asks, ask_volumes=ask_vols or [10000] * len(asks), name=name, source="test")


@pytest.fixture
def tick_factory():
    return make_tick
