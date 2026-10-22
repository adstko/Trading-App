"""Live trading loop: entries, risk limits, end-of-day flatten, kill switch."""
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from broker import (BUY, ET, SELL, Broker, DataError, DayTradeLimitError, OrderRejected)
from config import Config, load_config
from strategy import load_strategy
from trade_log import TradeLog, summary_text

log = logging.getLogger("bot")
MARKET_OPEN = dtime(9, 30)
ONE_MIN = pd.Timedelta(minutes=1)


class StartupError(Exception):
    pass


@dataclass
class OpenTrade:
    symbol: str
    side: str                       # 'buy' (long) or 'sell' (short)
    qty: float
    opened_at: datetime
    notional: float
    entry_price: Optional[float] = None
    last_price: Optional[float] = None
    seen: bool = False              # has shown up as a position at the broker
    attempts: int = 0               # tries at finding its exit fill


def _opposite(side):
    return SELL if side == BUY else BUY


def _et_str(t: datetime) -> str:
    return t.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")


class Bot:
    retry_delay = 2  # seconds between close attempts

    def __init__(self, cfg: Config, broker: Broker, cfg_path: str, folder: Path):
        self.cfg, self.broker, self.cfg_path = cfg, broker, cfg_path
        self.stop_file = Path(folder) / "STOP"
        self.tradelog = TradeLog(Path(folder) / "trades.csv")
        self.strategy = load_strategy(cfg.strategy, cfg.strategy_params)
        self.trades: dict[str, OpenTrade] = {}   # open or pending positions
        self.unlogged: list[OpenTrade] = []      # closed, waiting for exit fill info
        self.day = None
        self.finished = False                    # done trading for the day
        self.no_new_trades: Optional[str] = None
        self.blocked: set[str] = set()           # symbols the broker rejected today
        self.last_eval: dict[str, pd.Timestamp] = {}

    # ---------------- startup / main loop ----------------
    def startup(self):
        if self.stop_file.exists():
            raise StartupError(f"A STOP file exists ({self.stop_file}). Delete it to start the bot.")
        account = self.broker.get_account()
        if account.trading_blocked:
            raise StartupError("The broker reports trading is blocked on this account.")
        if self.cfg.allow_short and not account.shorting_enabled:
            raise StartupError("allow_short is true but this account does not support short selling. "
                               "Enable shorting/margin with your broker, or set allow_short: false.")
        log.info("Mode: %s | equity $%.2f | buying power $%.2f | day trades used: %d",
                 self.cfg.mode.upper(), account.equity, account.buying_power, account.day_trade_count)
        positions = self.broker.get_positions()
        if positions:
            log.warning("LEFTOVER POSITIONS found at startup (may be unprotected): %s",
                        ", ".join(f"{p.symbol} {p.qty:+g}" for p in positions))
            self.sync_trades(positions)

    def run(self):
        self.startup()
        log.info("Bot running. Create a file named STOP in %s to close everything and quit.",
                 self.stop_file.parent)
        while True:
            if self.stop_file.exists():
                log.warning("STOP file found: closing everything and shutting down.")
                ok = self.flatten("kill switch", attempts=10)
                self.log_closed(force=True)
                self.print_summary()
                if not ok:
                    log.critical("COULD NOT CONFIRM ALL POSITIONS CLOSED. CHECK YOUR BROKER NOW.")
                return
            try:
                self.cycle()
            except Exception as e:  # API, data feed, or any bug: stop opening, try to get flat
                log.error("Failure (%s: %s). No new trades; trying to close all positions.",
                          type(e).__name__, e)
                self.flatten("API/data failure")
            self._sleep(self.cfg.poll_seconds)

    def _sleep(self, seconds):
        for _ in range(int(seconds)):
            if self.stop_file.exists():
                return
            time.sleep(1)

    # ---------------- one cycle ----------------
    def cycle(self):
        self.reload_config()
        clock = self.broker.get_clock()
        now = clock.now.astimezone(ET)
        if now.date() != self.day:
            self.new_day(now.date())
        if self.finished or not clock.is_open:
            return

        account = self.broker.get_account()
        positions = self.broker.get_positions()
        self.sync_trades(positions)
        self.log_closed()

        pnl = account.equity - account.last_equity
        if pnl <= -self.cfg.daily_loss_limit:
            self.end_day(f"daily loss limit hit (P&L ${pnl:,.2f})")
        elif now >= self.flatten_deadline(now, clock):
            self.end_day("end of day")
        elif now >= datetime.combine(now.date(), MARKET_OPEN, ET) + timedelta(minutes=self.cfg.skip_open_minutes):
            self.scan(now, account, positions)

    def flatten_deadline(self, now, clock) -> datetime:
        deadline = datetime.combine(now.date(), self.cfg.flatten_at, ET)
        close = clock.next_close.astimezone(ET)
        if close.date() == now.date():                    # early-close days
            deadline = min(deadline, close - timedelta(minutes=10))
        return deadline

    def new_day(self, day):
        self.day, self.finished = day, False
        self.no_new_trades, self.blocked, self.last_eval = None, set(), {}
        log.info("New trading day: %s", day)

    def end_day(self, reason):
        if not self.flatten(reason):
            return  # try again next cycle
        self.log_closed(force=True)
        self.finished = True
        self.print_summary()
        log.info("Done trading for today (%s).", reason)

    def reload_config(self):
        """config.yaml is re-read every cycle so limits/capital can change anytime."""
        try:
            new = load_config(self.cfg_path)
        except Exception as e:
            log.warning("Could not reload config (keeping the old one): %s", e)
            return
        locked = replace(new, mode=self.cfg.mode, broker=self.cfg.broker, data_feed=self.cfg.data_feed)
        if locked != new:
            log.warning("mode/broker/data_feed changes need a restart; ignoring them.")
        if locked != self.cfg:
            log.info("Config reloaded.")
            try:
                self.strategy = load_strategy(locked.strategy, locked.strategy_params)
            except Exception as e:
                log.warning("Bad strategy config (keeping the old config): %s", e)
                return
            self.cfg = locked

    # ---------------- entries ----------------
    def scan(self, now, account, positions):
        cfg = self.cfg
        if self.no_new_trades:
            return
        if cfg.day_trade_limit_enabled and account.day_trade_count + len(self.trades) >= cfg.max_day_trades:
            log.info("Day-trade limit reached (%d used + %d open); not opening new trades.",
                     account.day_trade_count, len(self.trades))
            return

        now_utc = now.astimezone(timezone.utc)
        start = datetime.combine(now.date(), MARKET_OPEN, ET)
        fresh = {}
        for sym, df in self.broker.get_bars(cfg.watchlist, start, now_utc).items():
            df = df[df.index + ONE_MIN <= now_utc]       # completed bars only
            if df.empty or (now_utc - (df.index[-1] + ONE_MIN)).total_seconds() > cfg.max_bar_age_seconds:
                continue
            fresh[sym] = df
        if not fresh:
            raise DataError("no fresh minute bars for any symbol")

        for sym, df in fresh.items():
            if self.no_new_trades:
                break
            if self.last_eval.get(sym) == df.index[-1] or len(df) < self.strategy.warmup:
                continue
            self.last_eval[sym] = df.index[-1]
            signal = self.strategy.signals(df).iloc[-1]
            if signal == 0 or sym in self.trades:
                continue
            side = BUY if signal > 0 else SELL
            if side == SELL and not (cfg.allow_short and account.shorting_enabled):
                continue
            if self.try_open(sym, side, float(df["close"].iloc[-1]), account, positions):
                account = self.broker.get_account()

    def try_open(self, sym, side, price, account, positions) -> bool:
        cfg = self.cfg
        if sym in self.blocked or len(self.trades) >= cfg.max_open_positions:
            return False
        qty = int(cfg.capital_per_trade // price)
        if qty < 1:
            log.info("%s: price $%.2f is above capital_per_trade; skipping.", sym, price)
            return False
        notional = qty * price
        if self.exposure(positions) + notional > cfg.max_total_capital_at_risk:
            log.info("%s: would exceed max_total_capital_at_risk; skipping.", sym)
            return False
        if notional > account.spendable:
            log.info("%s: $%.2f exceeds available funds ($%.2f); skipping.", sym, notional, account.spendable)
            return False

        sign = 1 if side == BUY else -1
        stop = round(price * (1 - sign * cfg.stop_loss_pct / 100), 2)
        target = round(price * (1 + sign * cfg.take_profit_pct / 100), 2)
        try:
            self.broker.place_bracket_order(sym, side, qty, stop, target)
        except DayTradeLimitError as e:
            self.no_new_trades = "day-trade limit"
            log.warning("Broker rejected %s for day-trade rules: %s. No more new trades today.", sym, e)
            return False
        except OrderRejected as e:
            self.blocked.add(sym)
            log.warning("Broker rejected %s order (%s); skipping %s for today.", sym, e, sym)
            return False
        log.info("OPEN %s %s %d @ ~%.2f  stop %.2f  target %.2f",
                 "LONG" if side == BUY else "SHORT", sym, qty, price, stop, target)
        self.trades[sym] = OpenTrade(sym, side, qty, datetime.now(timezone.utc), notional)
        return True

    def exposure(self, positions) -> float:
        """Dollars committed: live market value for filled positions, planned notional for pending ones."""
        held = {p.symbol: abs(p.market_value) for p in positions}
        return sum(held.get(sym, t.notional) for sym, t in self.trades.items())

    # ---------------- closing / bookkeeping ----------------
    def flatten(self, reason, attempts=5) -> bool:
        """Cancel all orders and close all positions. True once the broker shows no positions."""
        log.warning("FLATTEN: %s", reason)
        for _ in range(attempts):
            try:
                self.broker.cancel_all_orders()
                self.broker.close_all()
                time.sleep(self.retry_delay)
                positions = self.broker.get_positions()
                if not positions:
                    self.sync_trades([])
                    return True
                log.warning("Still open after close attempt: %s", ", ".join(p.symbol for p in positions))
            except Exception as e:
                log.error("Close attempt failed: %s", e)
                time.sleep(self.retry_delay)
        log.error("Could not confirm all positions closed.")
        return False

    def sync_trades(self, positions):
        """Match our records to the broker's positions; move vanished positions to the unlogged list."""
        now = datetime.now(timezone.utc)
        held = {p.symbol: p for p in positions}
        for sym, p in held.items():
            t = self.trades.get(sym)
            if t is None:
                log.warning("Tracking untracked position %s %+g", sym, p.qty)
                t = self.trades[sym] = OpenTrade(sym, BUY if p.qty > 0 else SELL, abs(p.qty), now,
                                                 abs(p.market_value))
            t.seen, t.qty = True, abs(p.qty)
            t.entry_price, t.last_price = p.avg_entry_price, p.current_price
        for sym in list(self.trades):
            t = self.trades[sym]
            if sym in held or (not t.seen and (now - t.opened_at).total_seconds() < 30):
                continue  # still open, or entry order still settling
            del self.trades[sym]
            self.unlogged.append(t)

    def log_closed(self, force=False):
        """Write closed trades to the CSV once their exit fill is available."""
        for t in list(self.unlogged):
            t.attempts += 1
            try:
                fills = self.broker.get_fills(t.symbol, t.opened_at - timedelta(seconds=10))
            except Exception as e:
                log.warning("Could not fetch fills for %s: %s", t.symbol, e)
                fills = []
            exits = [f for f in fills if f.side == _opposite(t.side)]
            entries = [f for f in fills if f.side == t.side]
            give_up = force or t.attempts >= 5
            if exits and (sum(f.qty for f in exits) >= t.qty or give_up):
                exit_price = sum(f.price * f.qty for f in exits) / sum(f.qty for f in exits)
                exit_time = exits[-1].time
            elif give_up:
                if t.last_price is None:
                    log.warning("%s: never filled / no data; dropping.", t.symbol)
                    self.unlogged.remove(t)
                    continue
                log.warning("%s: exit fill not found; using last seen price for the log.", t.symbol)
                exit_price, exit_time = t.last_price, datetime.now(timezone.utc)
            else:
                continue
            entry_price = t.entry_price or (entries[0].price if entries else None)
            if entry_price is None:
                log.warning("%s: entry never filled; nothing to log.", t.symbol)
                self.unlogged.remove(t)
                continue
            sign = 1 if t.side == BUY else -1
            pnl = round(sign * (exit_price - entry_price) * t.qty, 2)
            self.tradelog.append({
                "entry_time": _et_str(entries[0].time if entries else t.opened_at),
                "exit_time": _et_str(exit_time), "symbol": t.symbol,
                "side": "long" if t.side == BUY else "short", "qty": t.qty,
                "entry_price": round(entry_price, 4), "exit_price": round(exit_price, 4), "pnl": pnl})
            log.info("CLOSED %s %s %g: entry %.2f exit %.2f P&L $%.2f", "LONG" if t.side == BUY else "SHORT",
                     t.symbol, t.qty, entry_price, exit_price, pnl)
            self.unlogged.remove(t)

    def print_summary(self):
        day = self.day or datetime.now(ET).date()
        print(summary_text(self.tradelog.for_day(day), f"Daily summary {day}"))
