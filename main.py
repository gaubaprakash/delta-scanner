from __future__ import annotations

import argparse
import logging
import sys
import time

from scanner import (
    ExchangeScanError,
    MarketDataApiError,
    TelegramError,
    TIMEFRAME_SECONDS,
    create_scanner,
    print_telegram_chat_ids,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan Delta Exchange and BingX perpetuals for 15-minute "
            "RSI/MACD/EMA50 signals, plus optional hourly and daily scans."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run selected timeframe scan(s) once and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print new matches without sending Telegram messages or changing alert state.",
    )
    parser.add_argument(
        "--timeframe",
        choices=("15m", "1h", "1d", "all"),
        default="all",
        help="Timeframe to scan; defaults to all configured timeframes.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        action="store_true",
        help="Print chat IDs from Telegram updates; requires TELEGRAM_BOT_TOKEN.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()

    try:
        if args.telegram_chat_id:
            print_telegram_chat_ids()
            return 0

        timeframes = (
            tuple(TIMEFRAME_SECONDS)
            if args.timeframe == "all"
            else (args.timeframe,)
        )
        scanner, store = create_scanner(
            dry_run=args.dry_run,
            timeframes=timeframes,
        )
        try:
            if args.once:
                try:
                    scanner.scan(set(timeframes))
                except (ExchangeScanError, MarketDataApiError, TelegramError) as exc:
                    logging.getLogger("bingx_scanner").error("Scan failed: %s", exc)
                    return 1
                return 0

            next_scans = {timeframe: time.time() for timeframe in timeframes}
            while True:
                now = time.time()
                due_timeframes = {
                    timeframe
                    for timeframe, scan_at in next_scans.items()
                    if now >= scan_at
                }
                if due_timeframes:
                    try:
                        scanner.scan(due_timeframes)
                    except (ExchangeScanError, MarketDataApiError, TelegramError) as exc:
                        logging.getLogger("bingx_scanner").error("Scan failed: %s", exc)
                    completed_at = time.time()
                    for timeframe in due_timeframes:
                        interval = TIMEFRAME_SECONDS[timeframe]
                        next_scans[timeframe] = (
                            int(completed_at // interval) + 1
                        ) * interval

                next_scan = min(next_scans.values())
                try:
                    time.sleep(max(1, next_scan - time.time()))
                except KeyboardInterrupt:
                    return 0
        finally:
            store.close()
    except (ValueError, TelegramError) as exc:
        logging.getLogger("bingx_scanner").error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
