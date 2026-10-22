# Day-trading bot

Trades US stocks on 1-minute bars through Alpaca (paper or live). Default strategy: VWAP + fast/slow moving-average crossover. Every trade is a bracket order, so the stop-loss and take-profit live at the broker and still work if the bot crashes.

> **This can lose real money.** Run it in `paper` mode for a good while first. Nothing here is financial advice, and a backtest is not a promise of future results.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then paste your Alpaca keys
```

Paper and live use **different** Alpaca keys (`ALPACA_PAPER_*` and `ALPACA_LIVE_*` in `.env`). `.env` is git-ignored; keys are never in code.

## Run

```bash
python main.py                                                    # run the bot
python main.py backtest --start 2026-09-01 --end 2026-10-01      # backtest the strategy
python main.py backtest --start 2026-09-01 --end 2026-10-01 --symbols AAPL NVDA
pytest                                                            # tests (no keys needed)
```

Live mode (`mode: live` in `config.yaml`) makes you type `I understand this uses real money` at startup.

**Kill switch:** create a file named `STOP` in this folder. The bot cancels all orders, closes every position and exits. It will not start while `STOP` exists, so delete it first. (Ctrl-C just stops the bot; open positions keep their broker-side stop/target.)

## What the bot does

- Trades only while the market is open, and skips the first `skip_open_minutes` (5) after 9:30 ET.
- **LONG** when the last closed bar is above VWAP and the fast MA crosses above the slow MA. **SHORT** on the opposite (real short-sell orders). VWAP and the MAs restart each day, so the first signal comes about `slow_ma` minutes after the open.
- Whole shares only: `qty = floor(capital_per_trade / price)`.
- At **15:50 ET** (or 10 minutes before an early close) it cancels all orders, closes ALL positions, prints a daily summary, and idles until the next day. Nothing is held overnight.
- **At startup** it checks that shorting is supported (if `allow_short`) and refuses to start otherwise. It flags any leftover positions, tracks them, and closes them at the flatten time.
- **Daily loss limit:** today's P&L is the broker's `equity - last_equity`. At `-daily_loss_limit` it closes everything and stops trading until the next day (a restart does not reset it).
- **Before each order** it checks: `max_open_positions`, `max_total_capital_at_risk`, and available funds. Buying power is the limit on margin accounts, and settled cash as well on cash accounts. Orders that would break a limit are skipped, not shrunk.
- **Failures:** if any API call or the data feed fails (or all data is stale), it opens nothing and tries to close all positions. It resumes trading once calls succeed again.
- **Day trades:** if `day_trade_limit_enabled`, it stops opening new trades when `broker day-trade count + open positions >= max_day_trades` (each open position will become a day trade when closed). If the broker rejects an order for day-trade reasons, it logs that and stops opening new trades for the day.
- **Logs:** `trades.csv` (entry/exit time in ET, symbol, side, qty, entry, exit, P&L) and `bot.log`.

## Config (`config.yaml`)

`config.yaml` is re-read every cycle, so you can change `capital_per_trade`, limits, the watchlist, etc. while the bot runs. `mode`, `broker` and `data_feed` need a restart. Typos or invalid values are rejected (an invalid edit while running is ignored, with a warning).

| Option | Default | Meaning |
|---|---|---|
| `mode` | `paper` | `paper` or `live` |
| `broker` | `alpaca` | which adapter to use |
| `data_feed` | `iex` | `iex` (free; volume is a fraction of the market, so VWAP is IEX-only) or `sip` (paid) |
| `watchlist` | SPY, QQQ, AAPL, MSFT, NVDA, TSLA | symbols to trade |
| `strategy`, `strategy_params` | `vwap_ma_cross`, `fast_ma: 9`, `slow_ma: 21` | strategy and its settings |
| `allow_short` | `true` | allow short sells |
| `stop_loss_pct` / `take_profit_pct` | `1.0` / `2.0` | percent from entry, set on the bracket order |
| `capital_per_trade` | `1000` | dollars per trade |
| `max_open_positions` | `3` | |
| `max_total_capital_at_risk` | `3000` | hard cap on total dollars deployed across open positions (market value, long and short) |
| `daily_loss_limit` | `100` | dollars; required, cannot be disabled |
| `day_trade_limit_enabled` / `max_day_trades` | `true` / `3` | PDT-style limit; turn off if it doesn't apply to you |
| `skip_open_minutes` | `5` | no entries right after the open |
| `flatten_time` | `"15:50"` | ET; keep the quotes |
| `poll_seconds` | `20` | how often it checks |
| `max_bar_age_seconds` | `180` | older data = feed failure |
| `backtest_slippage_pct` | `0.01` | worse fills in backtests |

## Backtest notes

It uses the same strategy code as live trading. Each symbol is simulated separately, so portfolio-wide limits (max positions, total capital, daily loss) are **not** applied. Entries fill at the next bar's open, and if a bar touches both stop and target the stop is assumed to hit first. Results are written to `backtest_trades.csv`. Backtests are usually optimistic.

## Swapping the strategy

In `strategy.py`, subclass `Strategy`, implement `signals(bars)` (returns +1 / -1 / 0 per bar), add it to `STRATEGIES`, and set `strategy:` in `config.yaml`.

## Adding another broker

1. Create `my_broker.py` with a class that subclasses `broker.Broker` and implements every method (`get_bars`, `place_order`, `place_bracket_order`, `get_positions`, `close_all`, `cancel_all_orders`, `get_account`, plus `get_clock` and `get_fills`). Raise `BrokerError`/`DataError`/`OrderRejected`/`DayTradeLimitError` for failures.
2. Register it in `create_broker()` in `broker.py`.
3. Set `broker: my_broker` in `config.yaml`.

`alpaca_broker.py` is the reference implementation.

## Files

`main.py` (CLI) · `bot.py` (live loop and risk rules) · `broker.py` (interface) · `alpaca_broker.py` · `strategy.py` · `backtest.py` · `config.py` · `trade_log.py` · `tests/`

## Limitations to know about

- Short selling needs a margin account with shorting enabled, and each stock must be shortable. The bot skips a symbol for the day if the broker rejects it.
- The 3:50pm flatten only protects you while the bot is running. If the bot is down, the broker-side stop/target orders are your protection, and they expire at the end of the day (DAY orders).
- A market entry can fill slightly away from the price used for the stop/target levels.
