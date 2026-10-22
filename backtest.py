"""Backtest: runs the configured strategy over historical 1-minute bars.

Each symbol is simulated on its own (no portfolio-wide position/capital/daily-loss limits).
Entries fill at the open of the bar after the signal; stop/target use bar highs/lows, and if
both are touched in one bar the stop is assumed to hit first. Slippage hits entries, stops and
the end-of-day exit; take-profit limits fill at their price.
"""
import numpy as np
import pandas as pd

from broker import ET
from trade_log import FIELDS


def simulate(symbol, bars, strategy, cfg) -> list[dict]:
    if len(bars) < 2:
        return []
    sig = strategy.signals(bars).to_numpy()
    et = bars.index.tz_convert(ET)
    day, mins = np.array(et.date), np.array(et.hour * 60 + et.minute)
    o, h, l, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    first_ok = 9 * 60 + 30 + cfg.skip_open_minutes
    flat = cfg.flatten_at.hour * 60 + cfg.flatten_at.minute
    slip = cfg.backtest_slippage_pct / 100
    sl, tp = cfg.stop_loss_pct / 100, cfg.take_profit_pct / 100

    trades, pos = [], None

    def close(i, price):
        nonlocal pos
        s = pos["sign"]
        trades.append({"entry_time": pos["time"], "exit_time": et[i].strftime("%Y-%m-%d %H:%M:%S"),
                       "symbol": symbol, "side": "long" if s > 0 else "short", "qty": pos["qty"],
                       "entry_price": round(pos["entry"], 4), "exit_price": round(price, 4),
                       "pnl": round(s * (price - pos["entry"]) * pos["qty"], 2)})
        pos = None

    for i in range(len(bars)):
        if pos and mins[i] >= flat:
            close(i, o[i] * (1 - pos["sign"] * slip))
        if (pos is None and i > 0 and sig[i - 1] != 0 and day[i - 1] == day[i]
                and first_ok <= mins[i] < flat and (sig[i - 1] > 0 or cfg.allow_short)):
            s = int(sig[i - 1])
            entry = o[i] * (1 + s * slip)
            qty = int(cfg.capital_per_trade // entry)
            if qty >= 1:
                base = c[i - 1]
                pos = {"sign": s, "qty": qty, "entry": entry, "time": et[i].strftime("%Y-%m-%d %H:%M:%S"),
                       "stop": base * (1 - s * sl), "target": base * (1 + s * tp)}
        if pos:
            s = pos["sign"]
            stop_hit = l[i] <= pos["stop"] if s > 0 else h[i] >= pos["stop"]
            target_hit = h[i] >= pos["target"] if s > 0 else l[i] <= pos["target"]
            if stop_hit:
                gap = min(o[i], pos["stop"]) if s > 0 else max(o[i], pos["stop"])
                close(i, gap * (1 - s * slip))
            elif target_hit:
                close(i, max(o[i], pos["target"]) if s > 0 else min(o[i], pos["target"]))
        if pos and (i == len(bars) - 1 or day[i + 1] != day[i]):  # last bar of the day
            close(i, c[i] * (1 - pos["sign"] * slip))
    return trades


def run_backtest(broker, cfg, strategy, start, end, symbols=None) -> pd.DataFrame:
    symbols = symbols or cfg.watchlist
    rows = []
    for sym, bars in broker.get_bars(symbols, start, end).items():
        rows += simulate(sym, bars, strategy, cfg)
    df = pd.DataFrame(rows, columns=FIELDS)
    return df.sort_values("entry_time").reset_index(drop=True)
