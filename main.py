from __future__ import annotations

import argparse
import logging
import sys

from scanner import (
    MarketDataApiError,
    TelegramError,
    create_scanner,
    print_telegram_chat_ids,
    wait_for_next_scan,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan BingX perpetuals for 15-minute RSI/MACD/EMA50 signals."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan and exit instead of polling every 15 minutes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print new matches without sending Telegram messages or changing alert state.",
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

        scanner, store = create_scanner(dry_run=args.dry_run)
        try:
            while True:
                try:
                    scanner.scan()
                except (MarketDataApiError, TelegramError) as exc:
                    logging.getLogger("bingx_scanner").error("Scan failed: %s", exc)
                    if args.once:
                        return 1
                if args.once:
                    return 0
                wait_for_next_scan()
        finally:
            store.close()
    except (ValueError, TelegramError) as exc:
        logging.getLogger("bingx_scanner").error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
