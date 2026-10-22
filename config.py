"""Loads and validates config.yaml. Unknown keys are rejected so typos can't silently change risk settings."""
from dataclasses import dataclass, field, fields
from datetime import time

import yaml


@dataclass(frozen=True)
class Config:
    mode: str = "paper"                       # paper | live
    broker: str = "alpaca"
    data_feed: str = "iex"                    # iex (free) | sip (paid)
    watchlist: list = field(default_factory=lambda: ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA"])
    strategy: str = "vwap_ma_cross"
    strategy_params: dict = field(default_factory=lambda: {"fast_ma": 9, "slow_ma": 21})
    allow_short: bool = True
    stop_loss_pct: float = 1.0                # percent of entry price
    take_profit_pct: float = 2.0
    capital_per_trade: float = 1000.0         # dollars
    max_open_positions: int = 3
    max_total_capital_at_risk: float = 3000.0 # dollars deployed across all open positions
    daily_loss_limit: float = 100.0           # dollars
    day_trade_limit_enabled: bool = True
    max_day_trades: int = 3
    skip_open_minutes: int = 5
    flatten_time: str = "15:50"               # ET, close everything
    poll_seconds: int = 20
    max_bar_age_seconds: int = 180            # older data counts as a feed problem
    backtest_slippage_pct: float = 0.01

    def __post_init__(self):
        def need(ok, msg):
            if not ok:
                raise ValueError(f"config.yaml: {msg}")
        need(self.mode in ("paper", "live"), "mode must be 'paper' or 'live'")
        need(self.data_feed in ("iex", "sip"), "data_feed must be 'iex' or 'sip'")
        need(self.watchlist, "watchlist is empty")
        need(0 < self.stop_loss_pct < 100, "stop_loss_pct must be between 0 and 100")
        need(self.take_profit_pct > 0, "take_profit_pct must be > 0")
        need(self.capital_per_trade > 0, "capital_per_trade must be > 0")
        need(self.max_open_positions >= 1, "max_open_positions must be >= 1")
        need(self.max_total_capital_at_risk >= self.capital_per_trade,
             "max_total_capital_at_risk must be >= capital_per_trade")
        need(self.daily_loss_limit > 0, "daily_loss_limit must be > 0 (it cannot be turned off)")
        need(self.max_day_trades >= 1, "max_day_trades must be >= 1")
        need(self.skip_open_minutes >= 0, "skip_open_minutes must be >= 0")
        need(self.poll_seconds >= 1, "poll_seconds must be >= 1")
        need(isinstance(self.flatten_time, str), 'flatten_time must be quoted, e.g. "15:50"')
        try:
            time.fromisoformat(self.flatten_time)
        except ValueError:
            need(False, 'flatten_time must look like "15:50"')

    @property
    def flatten_at(self) -> time:
        return time.fromisoformat(self.flatten_time)


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    unknown = set(raw) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError(f"config.yaml: unknown option(s): {', '.join(sorted(unknown))}")
    return Config(**raw)
