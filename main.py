"""Entry point.   python main.py            -> run the bot
                 python main.py backtest --start 2026-09-01 --end 2026-10-01 [--symbols AAPL MSFT]"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from backtest import run_backtest
from bot import Bot, StartupError
from broker import ET, BrokerError, create_broker
from config import load_config
from strategy import load_strategy
from trade_log import summary_text

HERE = Path(__file__).resolve().parent
LIVE_PHRASE = "I understand this uses real money"


def confirm_live():
    print("\n*** LIVE MODE: this bot will place orders with REAL MONEY. ***")
    if input(f'Type "{LIVE_PHRASE}" to continue: ').strip() != LIVE_PHRASE:
        sys.exit("Confirmation not given. Exiting.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", nargs="?", default="run", choices=["run", "backtest"])
    p.add_argument("--config", default=str(HERE / "config.yaml"))
    p.add_argument("--start", help="backtest start date YYYY-MM-DD")
    p.add_argument("--end", help="backtest end date YYYY-MM-DD")
    p.add_argument("--symbols", nargs="+", help="backtest symbols (default: watchlist)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "bot.log")])
    load_dotenv(HERE / ".env")

    try:
        cfg = load_config(args.config)
        if args.command == "backtest":
            if not (args.start and args.end):
                sys.exit("backtest needs --start and --end (YYYY-MM-DD)")
            start, end = (datetime.fromisoformat(d).replace(tzinfo=ET) for d in (args.start, args.end))
            trades = run_backtest(create_broker(cfg), cfg, load_strategy(cfg.strategy, cfg.strategy_params),
                                  start, end, args.symbols)
            trades.to_csv(HERE / "backtest_trades.csv", index=False)
            print(summary_text(trades, f"Backtest {args.start} to {args.end}"))
            print("Trades saved to backtest_trades.csv")
            return

        if cfg.mode == "live":
            confirm_live()
        Bot(cfg, create_broker(cfg), args.config, HERE).run()
    except (StartupError, BrokerError, ValueError) as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        print("\nStopped. Any open positions are still protected by their broker-side stop/target orders. "
              "Create a STOP file (or close them at your broker) to flatten.")


if __name__ == "__main__":
    main()
