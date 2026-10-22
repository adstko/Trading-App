"""Alpaca adapter (paper and live). Keys come from the environment / .env file."""
import os
from datetime import datetime
from typing import Optional

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, LimitOrderRequest, MarketOrderRequest,
                                     StopLossRequest, TakeProfitRequest)

from broker import (BAR_COLUMNS, BUY, Account, Broker, BrokerError, DataError, DayTradeLimitError,
                    Fill, MarketClock, OrderInfo, OrderRejected, Position)


class AlpacaBroker(Broker):
    def __init__(self, mode: str, data_feed: str = "iex"):
        prefix = "ALPACA_PAPER" if mode == "paper" else "ALPACA_LIVE"
        key, secret = os.getenv(f"{prefix}_API_KEY"), os.getenv(f"{prefix}_SECRET_KEY")
        if not key or not secret:
            raise BrokerError(f"Missing {prefix}_API_KEY / {prefix}_SECRET_KEY. Put them in your .env file.")
        self.trading = TradingClient(key, secret, paper=(mode == "paper"))
        self.data = StockHistoricalDataClient(key, secret)
        self.feed = DataFeed.SIP if data_feed == "sip" else DataFeed.IEX

    @staticmethod
    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            raise BrokerError(f"Alpaca error: {e}") from e

    # ---- data ----
    def get_bars(self, symbols, start, end=None):
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Minute,
                               start=start, end=end, feed=self.feed)
        try:
            df = self.data.get_stock_bars(req).df
        except Exception as e:
            raise DataError(f"Alpaca bars error: {e}") from e
        out = {s: pd.DataFrame(columns=BAR_COLUMNS) for s in symbols}
        if df is not None and not df.empty:
            for sym, g in df.groupby(level=0):
                out[sym] = g.droplevel(0)[BAR_COLUMNS]
        return out

    def get_clock(self):
        c = self._call(self.trading.get_clock)
        return MarketClock(now=c.timestamp, is_open=c.is_open, next_open=c.next_open, next_close=c.next_close)

    # ---- account ----
    def get_account(self):
        a = self._call(self.trading.get_account)
        return Account(
            equity=float(a.equity), last_equity=float(a.last_equity), cash=float(a.cash),
            buying_power=float(a.buying_power), is_margin=float(a.multiplier or 1) > 1,
            shorting_enabled=bool(a.shorting_enabled),
            trading_blocked=bool(a.trading_blocked or a.account_blocked),
            day_trade_count=int(a.daytrade_count or 0))

    def get_positions(self):
        return [Position(p.symbol, float(p.qty), float(p.avg_entry_price),
                         float(p.current_price), float(p.market_value))
                for p in self._call(self.trading.get_all_positions)]

    # ---- orders ----
    def _submit(self, req) -> OrderInfo:
        try:
            o = self.trading.submit_order(req)
        except APIError as e:
            msg = str(e)
            if "day trad" in msg.lower():
                raise DayTradeLimitError(msg) from e
            if getattr(e, "status_code", None) in (403, 422):
                raise OrderRejected(msg) from e
            raise BrokerError(f"Alpaca order error: {msg}") from e
        except Exception as e:
            raise BrokerError(f"Alpaca order error: {e}") from e
        return OrderInfo(str(o.id), o.symbol, o.side.value, float(o.qty))

    @staticmethod
    def _side(side):
        return OrderSide.BUY if side == BUY else OrderSide.SELL

    def place_order(self, symbol, side, qty, limit_price=None):
        common = dict(symbol=symbol, qty=qty, side=self._side(side), time_in_force=TimeInForce.DAY)
        if limit_price is None:
            return self._submit(MarketOrderRequest(**common))
        return self._submit(LimitOrderRequest(limit_price=limit_price, **common))

    def place_bracket_order(self, symbol, side, qty, stop_price, take_profit_price):
        return self._submit(MarketOrderRequest(
            symbol=symbol, qty=qty, side=self._side(side), time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price)))

    def cancel_all_orders(self):
        self._call(self.trading.cancel_orders)

    def close_all(self):
        self._call(self.trading.close_all_positions, cancel_orders=True)

    def get_fills(self, symbol, since):
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], after=since,
                               limit=100, nested=False)
        fills = [Fill(o.symbol, o.side.value, float(o.filled_qty), float(o.filled_avg_price), o.filled_at)
                 for o in self._call(self.trading.get_orders, req)
                 if o.filled_qty and float(o.filled_qty) > 0 and o.filled_avg_price]
        return sorted(fills, key=lambda f: f.time)
