from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from indicators import Indicators, calculate_indicators, matching_setups


LOGGER = logging.getLogger("bingx_scanner")
INTERVAL_SECONDS = 15 * 60
CANDLE_COUNT = 120
MIN_CANDLES = 50
SETUP_LABELS = {
    "overbought_bullish": "RSI > 70 and bullish MACD",
    "oversold_bearish": "RSI < 30 and bearish MACD",
}


class MarketDataApiError(RuntimeError):
    pass


class TelegramError(RuntimeError):
    pass


def load_environment_file(path: Path | None = None) -> None:
    env_path = path or Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid .env entry on line {line_number}")

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"Invalid .env variable name on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class Product:
    symbol: str
    contract_type: str


@dataclass(frozen=True)
class ProductScan:
    product: Product
    candle_time: int
    indicators: Indicators
    matches: dict[str, bool]


@dataclass(frozen=True)
class Signal:
    product: Product
    setup: str
    candle_time: int
    indicators: Indicators


class BingXClient:
    def __init__(self, base_url: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urlencode(params)
        url = f"{self.base_url}{path}?{query}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as exc:
            raise MarketDataApiError(f"BingX API returned HTTP {exc.code} for {path}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MarketDataApiError(f"BingX API request failed for {path}: {exc}") from exc

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataApiError(f"BingX API returned invalid JSON for {path}") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("msg") if isinstance(payload, dict) else None
            raise MarketDataApiError(
                f"BingX API reported failure for {path}: code={code}, message={message}"
            )
        return payload

    def live_products(self) -> list[Product]:
        payload = self._get_json("/openApi/swap/v2/quote/contracts", {})
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise MarketDataApiError("BingX contract response did not contain a data list")

        products = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if row.get("status") == 1 and isinstance(symbol, str) and symbol:
                products.append(Product(symbol=symbol, contract_type="perpetual"))
        return products

    def candles(self, symbol: str, now: int) -> list[tuple[int, float]]:
        payload = self._get_json(
            "/openApi/swap/v2/quote/klines",
            {"symbol": symbol, "interval": "15m", "limit": CANDLE_COUNT},
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise MarketDataApiError(
                f"BingX candle response for {symbol} did not contain a data list"
            )

        candles: dict[int, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                candle_time_ms = int(row["time"])
                close = float(row["close"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataApiError(
                    f"BingX returned a malformed candle for {symbol}"
                ) from exc
            if candle_time_ms + INTERVAL_SECONDS * 1000 <= now * 1000 and close > 0:
                candles[candle_time_ms // 1000] = close

        return sorted(candles.items())[-CANDLE_COUNT:]


class DeltaExchangeClient:
    def __init__(self, base_url: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += f"?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as exc:
            raise MarketDataApiError(f"Delta Exchange API returned HTTP {exc.code} for {path}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MarketDataApiError(f"Delta Exchange API request failed for {path}: {exc}") from exc

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataApiError(f"Delta Exchange API returned invalid JSON for {path}") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise MarketDataApiError(
                f"Delta Exchange API reported failure for {path}: {error}"
            )
        return payload

    def live_products(self) -> list[Product]:
        payload = self._get_json("/v2/products", {})
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise MarketDataApiError("Delta Exchange product response did not contain a result list")

        configured_symbols = {
            symbol.strip().upper()
            for symbol in os.environ.get("DELTA_SYMBOLS", "").split(",")
            if symbol.strip()
        }
        products = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if (
                isinstance(symbol, str)
                and symbol
                and (not configured_symbols or symbol.upper() in configured_symbols)
                and row.get("contract_type") == "perpetual_futures"
                and row.get("state") == "live"
                and row.get("trading_status") == "operational"
            ):
                products.append(Product(symbol=symbol, contract_type="perpetual"))
        return products

    def candles(self, symbol: str, now: int) -> list[tuple[int, float]]:
        start = now - (30 * 60 * 60)
        payload = self._get_json(
            "/v2/history/candles",
            {"resolution": "15m", "symbol": symbol, "start": start, "end": now},
        )
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise MarketDataApiError(
                f"Delta Exchange candle response for {symbol} did not contain a result list"
            )

        candles: dict[int, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                candle_time = int(row["time"])
                close = float(row["close"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataApiError(
                    f"Delta Exchange returned a malformed candle for {symbol}"
                ) from exc
            if candle_time + INTERVAL_SECONDS <= now and close > 0:
                candles[candle_time] = close

        return sorted(candles.items())[-CANDLE_COUNT:]


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 20) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def _request(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        body = None if data is None else json.dumps(data).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read())
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            description = (
                error_payload.get("description")
                if isinstance(error_payload, dict)
                else None
            )
            detail = f": {description}" if isinstance(description, str) else ""
            raise TelegramError(
                f"Telegram {method} returned HTTP {exc.code}{detail}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TelegramError(
                f"Telegram {method} request failed ({type(exc).__name__})"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError(f"Telegram {method} returned invalid JSON") from exc

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            description = payload.get("description", "unknown Telegram API error")
            raise TelegramError(f"Telegram {method} failed: {description}")
        return payload

    def send(self, message: str) -> None:
        self._request(
            "sendMessage",
            {"chat_id": self.chat_id, "text": message, "disable_web_page_preview": True},
        )

    def print_chat_ids(self) -> None:
        payload = self._request("getUpdates")
        updates = payload.get("result", [])
        chat_ids = {
            str(message["chat"]["id"])
            for update in updates
            for message in (update.get("message") or update.get("channel_post") or [],)
            if isinstance(message, dict) and isinstance(message.get("chat"), dict)
            and "id" in message["chat"]
        }
        if not chat_ids:
            print("No chats found. Send /start to your bot, then run this command again.")
            return
        print("Telegram chat IDs:")
        for chat_id in sorted(chat_ids):
            print(chat_id)


class AlertStore:
    def __init__(self, database_path: Path) -> None:
        self.connection = sqlite3.connect(database_path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS active_alerts (
                symbol TEXT NOT NULL,
                setup TEXT NOT NULL,
                active INTEGER NOT NULL,
                PRIMARY KEY (symbol, setup)
            )
            """
        )
        self.connection.commit()

    def is_active(self, symbol: str, setup: str) -> bool:
        row = self.connection.execute(
            "SELECT active FROM active_alerts WHERE symbol = ? AND setup = ?",
            (symbol, setup),
        ).fetchone()
        return row is not None and row[0] == 1

    def set_inactive(self, symbol: str, setup: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO active_alerts (symbol, setup, active)
                VALUES (?, ?, 0)
                ON CONFLICT(symbol, setup) DO UPDATE SET active = 0
                """,
                (symbol, setup),
            )

    def set_active(self, signals: list[Signal]) -> None:
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO active_alerts (symbol, setup, active)
                VALUES (?, ?, 1)
                ON CONFLICT(symbol, setup) DO UPDATE SET active = 1
                """,
                [(signal.product.symbol, signal.setup) for signal in signals],
            )

    def close(self) -> None:
        self.connection.close()


def _format_messages(signals: list[Signal]) -> list[tuple[str, list[Signal]]]:
    header = "Crypto 15-minute signal (completed candle)"
    messages: list[tuple[str, list[Signal]]] = []
    current_batch: list[Signal] = []
    current = header
    for signal in signals:
        candle_at = datetime.fromtimestamp(signal.candle_time, timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        entry = (
            f"{signal.product.symbol}: {SETUP_LABELS[signal.setup]}\n"
            f"RSI {signal.indicators.rsi:.2f} | "
            f"MACD {signal.indicators.macd:.6g} | "
            f"signal {signal.indicators.macd_signal:.6g}\n"
            f"Close {signal.indicators.close:.6g} | "
            f"EMA50 {signal.indicators.ema50:.6g}\n"
            f"Candle: {candle_at}"
        )
        if len(current) + 2 + len(entry) > 3500 and current != header:
            messages.append((current, current_batch))
            current_batch = []
            current = header
        current += f"\n\n{entry}"
        current_batch.append(signal)
    if current != header:
        messages.append((current, current_batch))
    return messages


class Scanner:
    def __init__(
        self,
        market_data: BingXClient,
        store: AlertStore,
        telegram: TelegramClient | None,
        dry_run: bool = False,
    ) -> None:
        self.market_data = market_data
        self.store = store
        self.telegram = telegram
        self.dry_run = dry_run

    def _scan_product(self, product: Product, now: int) -> ProductScan | None:
        candles = self.market_data.candles(product.symbol, now)
        if len(candles) < MIN_CANDLES:
            LOGGER.warning(
                "%s skipped: received %d completed candles; need at least %d",
                product.symbol,
                len(candles),
                MIN_CANDLES,
            )
            return None

        indicators = calculate_indicators([close for _, close in candles])
        return ProductScan(
            product=product,
            candle_time=candles[-1][0],
            indicators=indicators,
            matches=matching_setups(indicators),
        )

    def scan(self) -> None:
        products = self.market_data.live_products()
        if not products:
            LOGGER.warning("BingX returned no live perpetual contracts")
            return

        now = int(time.time())
        scans: list[ProductScan] = []
        failures = 0
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self._scan_product, product, now): product
                for product in products
            }
            for future in as_completed(futures):
                product = futures[future]
                try:
                    result = future.result()
                except MarketDataApiError as exc:
                    failures += 1
                    LOGGER.warning("%s candle scan failed: %s", product.symbol, exc)
                    continue
                if result is not None:
                    scans.append(result)

        if failures:
            LOGGER.warning(
                "%d of %d contract candle requests failed; existing alerts for those "
                "products were left unchanged",
                failures,
                len(products),
            )

        new_signals: list[Signal] = []
        for result in scans:
            for setup, matches in result.matches.items():
                if not matches:
                    self.store.set_inactive(result.product.symbol, setup)
                elif not self.store.is_active(result.product.symbol, setup):
                    new_signals.append(
                        Signal(
                            product=result.product,
                            setup=setup,
                            candle_time=result.candle_time,
                            indicators=result.indicators,
                        )
                    )

        if not new_signals:
            LOGGER.info(
                "Scan complete: %d contracts checked, no new signals",
                len(scans),
            )
            return

        if self.dry_run:
            print("\n\n".join(message for message, _ in _format_messages(new_signals)))
            LOGGER.info("Dry run: %d new signals; no alerts or state changes", len(new_signals))
            return
        if self.telegram is None:
            raise TelegramError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to send alerts")

        messages = _format_messages(new_signals)
        for message, batch in messages:
            self.telegram.send(message)
            self.store.set_active(batch)
        LOGGER.info(
            "Scan complete: %d contracts checked, sent %d new signals in %d messages",
            len(scans),
            len(new_signals),
            len(messages),
        )


def telegram_client_from_environment() -> TelegramClient:
    load_environment_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise TelegramError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; see README.md for setup"
        )
    return TelegramClient(token, chat_id)


def print_telegram_chat_ids() -> None:
    load_environment_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramError("Set TELEGRAM_BOT_TOKEN before requesting Telegram chat IDs")
    TelegramClient(token, "").print_chat_ids()


def wait_for_next_scan() -> None:
    now = time.time()
    next_scan = (int(now // INTERVAL_SECONDS) + 1) * INTERVAL_SECONDS
    time.sleep(max(1, next_scan - now))


def create_scanner(dry_run: bool = False) -> tuple[Scanner, AlertStore]:
    telegram = None if dry_run else telegram_client_from_environment()
    exchange = os.environ.get("SCANNER_EXCHANGE", "delta").strip().lower()
    if exchange == "delta":
        base_url = os.environ.get(
            "DELTA_API_BASE_URL",
            "https://api.india.delta.exchange",
        ).strip()
        if not base_url.startswith("https://"):
            raise ValueError("DELTA_API_BASE_URL must use HTTPS")
        store = AlertStore(Path(__file__).with_name("delta_scanner_state.sqlite3"))
        return Scanner(DeltaExchangeClient(base_url), store, telegram, dry_run), store
    if exchange == "bingx":
        base_url = os.environ.get(
            "BINGX_API_BASE_URL",
            "https://open-api.bingx.com",
        ).strip()
        if not base_url.startswith("https://"):
            raise ValueError("BINGX_API_BASE_URL must use HTTPS")
        store = AlertStore(Path(__file__).with_name("bingx_scanner_state.sqlite3"))
        return Scanner(BingXClient(base_url), store, telegram, dry_run), store
    raise ValueError("SCANNER_EXCHANGE must be 'delta' or 'bingx'")
