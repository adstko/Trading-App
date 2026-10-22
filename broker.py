"""Broker interface. The bot only talks to this module.

To add another broker: subclass Broker, implement every method, and register it in
create_broker() at the bottom.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
BUY, SELL = "buy", "sell"
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


class BrokerError(Exception):
    """API call failed (network, server error, auth...)."""


class DataError(BrokerError):
    """Market data could not be fetched or is unusable."""


class OrderRejected(BrokerError):
    """The broker understood the order but refused it."""


class DayTradeLimitError(OrderRejected):
    """Order refused because of day-trade (PDT) rules."""


@dataclass
class Account:
    equity: float
    last_equity: float          # equity at previous close; equity - last_equity = today's P&L
    cash: float
    buying_power: float
    is_margin: bool
    shorting_enabled: bool
    trading_blocked: bool
    day_trade_count: int        # day trades in the broker's rolling window

    @property
    def spendable(self) -> float:
        """Most we may commit to a new position: buying power, and also settled cash on cash accounts."""
        return self.buying_power if self.is_margin else min(self.buying_power, self.cash)


@dataclass
class Position:
    symbol: str
    qty: float                  # negative = short
    avg_entry_price: float
    current_price: float
    market_value: float         # negative for shorts


@dataclass
class OrderInfo:
    id: str
    symbol: str
    side: str
    qty: float


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    time: datetime


@dataclass
class MarketClock:
    now: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class Broker(ABC):
    @abstractmethod
    def get_bars(self, symbols: list[str], start: datetime, end: Optional[datetime] = None) -> dict[str, pd.DataFrame]:
        """1-minute bars per symbol. DataFrame indexed by tz-aware UTC bar-open time with
        columns open, high, low, close, volume. Missing symbols map to an empty frame.
        Raises DataError on failure."""

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int, limit_price: Optional[float] = None) -> OrderInfo:
        """Market order, or limit order if limit_price is given. side is 'buy' or 'sell'
        (a 'sell' with no position is a real short sale)."""

    @abstractmethod
    def place_bracket_order(self, symbol: str, side: str, qty: int,
                            stop_price: float, take_profit_price: float) -> OrderInfo:
        """Market entry with a stop-loss and a take-profit held at the broker."""

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def close_all(self) -> None:
        """Close every position (cancelling related orders)."""

    @abstractmethod
    def cancel_all_orders(self) -> None: ...

    @abstractmethod
    def get_account(self) -> Account: ...

    # Needed by the bot beyond the core list:
    @abstractmethod
    def get_clock(self) -> MarketClock:
        """Broker's market clock (handles holidays and early closes)."""

    @abstractmethod
    def get_fills(self, symbol: str, since: datetime) -> list[Fill]:
        """Filled orders for symbol submitted since `since`, oldest first (used to log exits)."""


def create_broker(cfg) -> Broker:
    if cfg.broker == "alpaca":
        from alpaca_broker import AlpacaBroker
        return AlpacaBroker(cfg.mode, cfg.data_feed)
    raise ValueError(f"Unknown broker '{cfg.broker}'. Register it in broker.create_broker().")
