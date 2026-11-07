from datetime import datetime

from quant.core.calendar import bar_minute, minutes_since_open
from quant.core.models import Direction
from quant.core.rules import Board, FeeModel, board_of, check_volume, exchange_of, limit_prices, round_buy_volume


def test_board_and_exchange():
    assert board_of("600000") == Board.SH_MAIN and exchange_of("600000") == "SH"
    assert board_of("688981") == Board.STAR and exchange_of("688981") == "SH"
    assert board_of("000001") == Board.SZ_MAIN and exchange_of("000001") == "SZ"
    assert board_of("300750") == Board.CHINEXT and exchange_of("300750") == "SZ"
    assert board_of("920118") == Board.BSE and exchange_of("920118") == "BJ"
    assert board_of("830799") == Board.BSE
    assert exchange_of("510300") == "SH"


def test_limit_prices():
    assert limit_prices("600000", 10.0) == (11.0, 9.0)
    assert limit_prices("600000", 10.0, "*ST 某某") == (10.5, 9.5)
    assert limit_prices("300750", 250.0) == (300.0, 200.0)
    assert limit_prices("688981", 90.0, "ST 科创") == (108.0, 72.0)  # 科创板 ST 仍是 20%
    assert limit_prices("920118", 10.0) == (13.0, 7.0)
    # 四舍五入到分：10.05 * 1.1 = 11.055 -> 11.06
    assert limit_prices("600000", 10.05) == (11.06, 9.05)


def test_volume_rules():
    assert check_volume("600000", Direction.BUY, 100) is None
    assert check_volume("600000", Direction.BUY, 150) is not None
    assert check_volume("688981", Direction.BUY, 100) is not None
    assert check_volume("688981", Direction.BUY, 201) is None
    # 卖出：零股只能一次清仓
    assert check_volume("600000", Direction.SELL, 50, available=150) is not None
    assert check_volume("600000", Direction.SELL, 150, available=150) is None
    assert check_volume("600000", Direction.SELL, 100, available=150) is None
    assert check_volume("600000", Direction.SELL, 200, available=150) is not None
    assert round_buy_volume("600000", 1999) == 1900
    assert round_buy_volume("688981", 199) == 0
    assert round_buy_volume("688981", 357) == 357


def test_fees():
    f = FeeModel()
    c, s, t = f.calc("600000", Direction.BUY, 10.0, 1000)  # 1 万元
    assert (c, s, t) == (5.0, 0.0, 0.1)                     # 佣金不足 5 元按 5 元
    c, s, t = f.calc("600000", Direction.SELL, 10.0, 100000)  # 100 万元
    assert c == 250.0 and s == 500.0 and t == 10.0
    assert f.calc("920118", Direction.SELL, 10.0, 1000)[2] == 0.0  # 北交所无过户费


def test_bar_minute_mapping():
    d = datetime(2026, 11, 9)
    assert bar_minute(d.replace(hour=9, minute=25, second=3)) == d.replace(hour=9, minute=30)
    assert bar_minute(d.replace(hour=9, minute=31, second=59)) == d.replace(hour=9, minute=31)
    assert bar_minute(d.replace(hour=11, minute=30, second=2)) == d.replace(hour=11, minute=29)
    assert bar_minute(d.replace(hour=12, minute=0)) is None
    assert bar_minute(d.replace(hour=15, minute=0, second=1)) == d.replace(hour=14, minute=59)
    assert bar_minute(d.replace(hour=15, minute=5)) is None
    assert minutes_since_open(d.replace(hour=13, minute=0)) == 120
    assert minutes_since_open(d.replace(hour=14, minute=59)) == 239
