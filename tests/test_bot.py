import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import simulate
from bot import Bot, StartupError
from broker import (ET, Account, Broker, BrokerError, DataError, DayTradeLimitError, MarketClock,
                    OrderInfo, Position)
from config import Config
from strategy import Strategy, VwapMaCross


class FakeBroker(Broker):
    def __init__(self, now_et="2026-10-22 10:30"):
        self.now = datetime.fromisoformat(now_et).replace(tzinfo=ET)
        self.account = Account(equity=10000, last_equity=10000, cash=10000, buying_power=20000,
                               is_margin=True, shorting_enabled=True, trading_blocked=False, day_trade_count=0)
        self.positions, self.orders, self.closed = [], [], 0
        self.fail_bars = False
        self.order_error = None
        self.price = 100.0

    def get_bars(self, symbols, start, end=None):
        if self.fail_bars:
            raise DataError("feed down")
        end_utc = self.now.astimezone(timezone.utc)
        idx = pd.date_range(end=end_utc - timedelta(minutes=1), periods=60, freq="1min")
        df = pd.DataFrame({"open": self.price, "high": self.price, "low": self.price,
                           "close": self.price, "volume": 1000.0}, index=idx)
        return {s: df for s in symbols}

    def place_order(self, *a, **k): raise NotImplementedError

    def place_bracket_order(self, symbol, side, qty, stop_price, take_profit_price):
        if self.order_error:
            raise self.order_error
        self.orders.append((symbol, side, qty, stop_price, take_profit_price))
        return OrderInfo("1", symbol, side, qty)

    def get_positions(self): return self.positions
    def close_all(self): self.closed += 1; self.positions = []
    def cancel_all_orders(self): pass
    def get_account(self): return self.account
    def get_fills(self, symbol, since): return []

    def get_clock(self):
        return MarketClock(self.now, True, self.now, self.now.replace(hour=16, minute=0))


class Always(Strategy):
    def __init__(self, sig): self.sig = sig
    def signals(self, bars): return pd.Series(self.sig, index=bars.index)


def make_bot(tmp_path, sig=1, broker=None, **cfg):
    cfg.setdefault("watchlist", ["AAA"])
    broker = broker or FakeBroker()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg))   # the bot reloads this file every cycle
    bot = Bot(Config(**cfg), broker, str(cfg_file), tmp_path)
    bot.retry_delay = 0
    bot.strategy = Always(sig)
    return bot, broker


def test_long_places_bracket_with_stop_and_target(tmp_path):
    bot, b = make_bot(tmp_path, sig=1, stop_loss_pct=1, take_profit_pct=2)
    bot.cycle()
    assert b.orders == [("AAA", "buy", 10, 99.0, 102.0)]


def test_short_uses_sell_with_inverted_levels(tmp_path):
    bot, b = make_bot(tmp_path, sig=-1)
    bot.cycle()
    assert b.orders == [("AAA", "sell", 10, 101.0, 98.0)]


def test_no_shorts_when_disabled(tmp_path):
    bot, b = make_bot(tmp_path, sig=-1, allow_short=False)
    bot.cycle()
    assert b.orders == []


def test_startup_refuses_when_shorting_unsupported(tmp_path):
    b = FakeBroker()
    b.account.shorting_enabled = False
    bot, _ = make_bot(tmp_path, broker=b)
    with pytest.raises(StartupError, match="short"):
        bot.startup()


def test_skips_first_minutes_after_open(tmp_path):
    bot, b = make_bot(tmp_path, broker=FakeBroker("2026-10-22 09:34"))
    bot.cycle()
    assert b.orders == []


def test_capital_cap_never_exceeded(tmp_path):
    bot, b = make_bot(tmp_path, watchlist=["A", "B", "C"], capital_per_trade=1000,
                      max_total_capital_at_risk=1500, max_open_positions=5)
    bot.cycle()
    assert len(b.orders) == 1


def test_max_open_positions(tmp_path):
    bot, b = make_bot(tmp_path, watchlist=["A", "B", "C"], max_open_positions=2)
    bot.cycle()
    assert len(b.orders) == 2


def test_buying_power_respected(tmp_path):
    b = FakeBroker()
    b.account.buying_power = 500
    bot, _ = make_bot(tmp_path, broker=b)
    bot.cycle()
    assert b.orders == []


def test_cash_account_limited_by_cash(tmp_path):
    b = FakeBroker()
    b.account.is_margin, b.account.cash = False, 500
    bot, _ = make_bot(tmp_path, broker=b)
    bot.cycle()
    assert b.orders == []


def test_daily_loss_limit_closes_everything_and_stops(tmp_path):
    b = FakeBroker()
    b.account.equity = 9890
    b.positions = [Position("AAA", 10, 100, 99, 990)]
    bot, _ = make_bot(tmp_path, broker=b, daily_loss_limit=100)
    bot.cycle()
    assert b.closed == 1 and bot.finished and b.orders == []
    bot.cycle()
    assert b.orders == []


def test_flatten_at_deadline(tmp_path):
    b = FakeBroker("2026-10-22 15:50")
    b.positions = [Position("AAA", 10, 100, 100, 1000)]
    bot, _ = make_bot(tmp_path, broker=b)
    bot.cycle()
    assert b.closed == 1 and bot.finished and b.orders == []


def test_day_trade_rejection_stops_new_trades(tmp_path):
    b = FakeBroker()
    b.order_error = DayTradeLimitError("pattern day trading protection")
    bot, _ = make_bot(tmp_path, broker=b, watchlist=["A", "B"])
    bot.cycle()
    assert bot.no_new_trades and b.orders == []


def test_day_trade_limit_config(tmp_path):
    b = FakeBroker()
    b.account.day_trade_count = 3
    bot, _ = make_bot(tmp_path, broker=b, max_day_trades=3)
    bot.cycle()
    assert b.orders == []
    bot2, b2 = make_bot(tmp_path, broker=FakeBroker(), day_trade_limit_enabled=False)
    b2.account.day_trade_count = 3
    bot2.cycle()
    assert len(b2.orders) == 1


def test_data_failure_blocks_trades_and_closes(tmp_path, monkeypatch):
    b = FakeBroker()
    b.fail_bars = True
    b.positions = [Position("ZZZ", 5, 100, 100, 500)]
    bot, _ = make_bot(tmp_path, broker=b)
    monkeypatch.setattr(bot, "_sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        bot.run()
    assert b.orders == [] and b.closed >= 1


def test_stop_file_closes_and_exits(tmp_path):
    b = FakeBroker()
    b.positions = [Position("AAA", 10, 100, 100, 1000)]
    bot, _ = make_bot(tmp_path, broker=b)
    bot.startup = lambda: None
    (tmp_path / "STOP").write_text("")
    bot.run()
    assert b.closed == 1


def test_startup_refuses_with_stop_file(tmp_path):
    bot, _ = make_bot(tmp_path)
    (tmp_path / "STOP").write_text("")
    with pytest.raises(StartupError):
        bot.startup()


def test_config_rejects_unknown_and_bad_values():
    with pytest.raises(ValueError):
        Config(stop_loss_pct=0)
    with pytest.raises(ValueError):
        Config(max_total_capital_at_risk=10, capital_per_trade=1000)


def _day_bars(prices, start="2026-10-22 09:30"):
    idx = pd.date_range(pd.Timestamp(start, tz=ET), periods=len(prices), freq="1min").tz_convert("UTC")
    p = np.array(prices, float)
    return pd.DataFrame({"open": p, "high": p + 0.05, "low": p - 0.05, "close": p, "volume": 1000.0}, index=idx)


def test_strategy_long_and_short_signals():
    up = _day_bars([100] * 30 + [100 + 0.5 * i for i in range(1, 15)])
    assert (VwapMaCross().signals(up) == 1).any()
    assert not (VwapMaCross().signals(up) == -1).any()
    down = _day_bars([100] * 30 + [100 - 0.5 * i for i in range(1, 15)])
    assert (VwapMaCross().signals(down) == -1).any()


def test_backtest_hits_take_profit_and_flattens_eod():
    bars = _day_bars([100] * 30 + [100 + 0.5 * i for i in range(1, 40)] + [150] * 300)
    cfg = Config(backtest_slippage_pct=0)
    trades = simulate("X", bars, VwapMaCross(), cfg)
    assert trades and all(t["exit_time"][:10] == "2026-10-22" for t in trades)
    assert trades[0]["side"] == "long" and trades[0]["pnl"] > 0
