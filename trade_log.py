"""CSV trade log and summary text."""
import csv
from pathlib import Path

import pandas as pd

FIELDS = ["entry_time", "exit_time", "symbol", "side", "qty", "entry_price", "exit_price", "pnl"]


class TradeLog:
    """Times are US/Eastern, 'YYYY-MM-DD HH:MM:SS'. side is 'long' or 'short'."""

    def __init__(self, path):
        self.path = Path(path)

    def append(self, row: dict):
        new = not self.path.exists()
        with open(self.path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)

    def for_day(self, day) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=FIELDS)
        df = pd.read_csv(self.path)
        return df[df["exit_time"].astype(str).str.startswith(day.isoformat())]


def summary_text(trades: pd.DataFrame, title: str) -> str:
    if trades.empty:
        return f"=== {title}: no trades ==="
    pnl = trades["pnl"]
    wins = int((pnl > 0).sum())
    lines = [f"=== {title} ===",
             f"Trades: {len(trades)}   Wins: {wins}   Losses: {int((pnl <= 0).sum())}   "
             f"Win rate: {wins / len(trades):.0%}",
             f"Total P&L: ${pnl.sum():,.2f}   Avg/trade: ${pnl.mean():,.2f}   "
             f"Best: ${pnl.max():,.2f}   Worst: ${pnl.min():,.2f}"]
    for sym, g in trades.groupby("symbol"):
        lines.append(f"  {sym:<6} {len(g):>3} trades  ${g['pnl'].sum():>10,.2f}")
    return "\n".join(lines)
