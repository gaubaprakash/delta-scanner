from __future__ import annotations

import json
import csv
import io
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name="IST")
TIMEFRAME_SECONDS = {
    "15m": INTERVAL_SECONDS,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
}
TIMEFRAME_LABELS = {"15m": "15-minute", "1h": "1-hour", "1d": "daily"}
TIMEFRAME_SYMBOL_ENV = {
    ("delta", "15m"): "DELTA_15M_SYMBOLS",
    ("delta", "1h"): "DELTA_1H_SYMBOLS",
    ("delta", "1d"): "DELTA_1D_SYMBOLS",
    ("bingx", "15m"): "BINGX_15M_SYMBOLS",
    ("bingx", "1h"): "BINGX_1H_SYMBOLS",
    ("bingx", "1d"): "BINGX_1D_SYMBOLS",
}
GOOGLE_SHEET_CSV_URL_DEFAULT = (
    "https://docs.google.com/spreadsheets/d/"
    "17UN3-QGibe42Uin-UuehGyQFj2qw9AiIBxa9dF3oqxE/"
    "export?format=csv&gid=0"
)
_sheet_symbol_cache: dict[str, tuple[float, dict[str, set[str]]]] = {}


def _normalized_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _is_active(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1", "active", "checked", "y"}


def _google_sheet_symbol_config() -> dict[str, set[str]]:
    now = time.time()
    url = os.environ.get("GOOGLE_SHEET_CSV_URL", "").strip()
    if not url:
        return {}

    cached = _sheet_symbol_cache.get("symbols")
    if cached and now - cached[0] < 30:
        return cached[1]

    request = Request(url, headers={"User-Agent": "delta-scanner/1.0"})
    try:
        with urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8-sig")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        LOGGER.warning("Could not refresh symbols from Google Sheet: %s", exc)
        return {}

    config: dict[str, set[str]] = {}
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        _sheet_symbol_cache["symbols"] = (now, config)
        return config

    first_row = [cell.strip() for cell in rows[0]]
    env_names = set(TIMEFRAME_SYMBOL_ENV.values())

    if len(first_row) == 1 and "=" in first_row[0]:
        for row in rows:
            if not row or "=" not in row[0]:
                continue
            name, value = row[0].split("=", 1)
            name = name.strip()
            if name in env_names:
                config[name] = {
                    symbol.strip().upper()
                    for symbol in value.split(",")
                    if symbol.strip()
                }
    elif any(header.lower().replace("_", " ").replace("-", " ") in {
        "source key", "exchange", "timeframe", "symbol"
    } for header in first_row):
        headers = {_normalized_header(header): index for index, header in enumerate(first_row)}
        source_key_index = headers.get("source_key")
        exchange_index = headers.get("exchange")
        timeframe_index = headers.get("timeframe")
        symbol_index = headers.get("symbol")
        active_index = headers.get("is_active")

        for row in rows[1:]:
            if active_index is not None and active_index < len(row) and not _is_active(row[active_index]):
                continue

            source_key = (
                row[source_key_index].strip()
                if source_key_index is not None and source_key_index < len(row)
                else ""
            )
            if not source_key and exchange_index is not None and timeframe_index is not None:
                exchange = row[exchange_index].strip().upper() if exchange_index < len(row) else ""
                timeframe = row[timeframe_index].strip().lower() if timeframe_index < len(row) else ""
                exchange_name = "DELTA" if exchange.startswith("DELTA") else "BINGX" if exchange.startswith("BINGX") else ""
                if exchange_name and timeframe in {"15m", "1h", "1d"}:
                    source_key = f"{exchange_name}_{timeframe.replace('m', 'M').replace('h', 'H').replace('d', 'D')}_SYMBOLS"

            if source_key not in env_names or symbol_index is None or symbol_index >= len(row):
                continue
            symbol = row[symbol_index].strip()
            if symbol:
                config.setdefault(source_key, set()).add(symbol.upper())

    _sheet_symbol_cache["symbols"] = (now, config)
    return config


SETUP_LABELS = {
    "overbought_bullish": "RSI > 70 and bullish MACD",
    "oversold_bearish": "RSI < 30 and bearish MACD",
}


def _configured_symbols(
    exchange: str,
    timeframe: str,
    legacy_name: str,
) -> set[str] | None:
    variable = TIMEFRAME_SYMBOL_ENV[(exchange, timeframe)]
    sheet_config = _google_sheet_symbol_config()
    if variable in sheet_config:
        return sheet_config[variable]

    if os.environ.get("GOOGLE_SHEET_CSV_URL", "").strip():
        LOGGER.warning(
            "Google Sheet did not provide an active list for %s; no contracts will be scanned",
            variable,
        )
        return None

    value = os.environ.get(variable)
    if value is None:
        value = os.environ.get(legacy_name, "")
    return {symbol.strip().upper() for symbol in value.split(",") if symbol.strip()}


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
    exchange: str = "Delta Exchange"
    timeframe: str = "15m"


class BingXClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 20,
        timeframe: str = "15m",
    ) -> None:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.timeframe = timeframe
        self.interval_seconds = TIMEFRAME_SECONDS[timeframe]

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

        configured_symbols = _configured_symbols("bingx", self.timeframe, "BINGX_SYMBOLS")
        if configured_symbols is None:
            return []
        products = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if (
                row.get("status") == 1
                and isinstance(symbol, str)
                and symbol
                and (not configured_symbols or symbol.upper() in configured_symbols)
            ):
                products.append(Product(symbol=symbol, contract_type="perpetual"))
        if configured_symbols:
            live_symbols = {product.symbol.upper() for product in products}
            unavailable_symbols = configured_symbols - live_symbols
            if unavailable_symbols:
                LOGGER.warning(
                    "Configured BingX symbols are not active perpetuals: %s",
                    ", ".join(sorted(unavailable_symbols)),
                )
        return products

    def candles(self, symbol: str, now: int) -> list[tuple[int, float]]:
        payload = self._get_json(
            "/openApi/swap/v2/quote/klines",
            {"symbol": symbol, "interval": self.timeframe, "limit": CANDLE_COUNT},
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
            if (
                candle_time_ms + self.interval_seconds * 1000 <= now * 1000
                and close > 0
            ):
                candles[candle_time_ms // 1000] = close

        return sorted(candles.items())[-CANDLE_COUNT:]


class DeltaExchangeClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 20,
        timeframe: str = "15m",
    ) -> None:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.timeframe = timeframe
        self.interval_seconds = TIMEFRAME_SECONDS[timeframe]

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

        configured_symbols = _configured_symbols("delta", self.timeframe, "DELTA_SYMBOLS")
        if configured_symbols is None:
            return []
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
        if configured_symbols:
            live_symbols = {product.symbol.upper() for product in products}
            unavailable_symbols = configured_symbols - live_symbols
            if unavailable_symbols:
                LOGGER.warning(
                    "Configured Delta symbols are not live perpetuals: %s",
                    ", ".join(sorted(unavailable_symbols)),
                )
        return products

    def candles(self, symbol: str, now: int) -> list[tuple[int, float]]:
        start = now - (CANDLE_COUNT * self.interval_seconds)
        payload = self._get_json(
            "/v2/history/candles",
            {
                "resolution": self.timeframe,
                "symbol": symbol,
                "start": start,
                "end": now,
            },
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
            if candle_time + self.interval_seconds <= now and close > 0:
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
        payload = self._request(
            "sendMessage",
            {"chat_id": self.chat_id, "text": message, "disable_web_page_preview": True},
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramError("Telegram sendMessage response omitted message details")
        chat = result.get("chat")
        message_id = result.get("message_id")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not isinstance(message_id, int) or not isinstance(chat_id, int):
            raise TelegramError("Telegram sendMessage response omitted message or chat ID")
        LOGGER.info(
            "Telegram accepted message_id=%d for chat_id=%d",
            message_id,
            chat_id,
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
    messages: list[tuple[str, list[Signal]]] = []
    for timeframe in ("15m", "1h", "1d"):
        for direction, setup in (
            ("Bullish", "overbought_bullish"),
            ("Bearish", "oversold_bearish"),
        ):
            header = (
                f"Crypto {TIMEFRAME_LABELS[timeframe]} "
                f"{direction.lower()} signals (completed candle)"
            )
            current_batch: list[Signal] = []
            current = header
            matching_signals = sorted(
                (
                    signal
                    for signal in signals
                    if signal.timeframe == timeframe and signal.setup == setup
                ),
                key=lambda signal: (signal.exchange, signal.product.symbol),
            )
            for signal in matching_signals:
                candle_at = datetime.fromtimestamp(
                    signal.candle_time,
                    IST_TIMEZONE,
                ).strftime("%Y-%m-%d %H:%M IST")
                entry = (
                    f"{signal.exchange} | {signal.product.symbol}: "
                    f"{SETUP_LABELS[signal.setup]}\n"
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
        exchange: str = "Delta Exchange",
        timeframe: str = "15m",
    ) -> None:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        self.market_data = market_data
        self.store = store
        self.telegram = telegram
        self.dry_run = dry_run
        self.exchange = exchange
        self.timeframe = timeframe
        self.last_scan_complete = False
        self.last_scan_matched = False

    def _scan_product(self, product: Product, now: int) -> ProductScan | None:
        candles = self.market_data.candles(product.symbol, now)
        if len(candles) < MIN_CANDLES:
            LOGGER.warning(
                "%s %s %s skipped: received %d completed candles; need at least %d",
                self.exchange,
                self.timeframe,
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

    def scan(self, defer_notifications: bool = False) -> list[Signal]:
        self.last_scan_complete = False
        self.last_scan_matched = False
        products = self.market_data.live_products()
        if not products:
            LOGGER.warning(
                "%s %s returned no live perpetual contracts",
                self.exchange,
                self.timeframe,
            )
            self.last_scan_complete = True
            return []

        LOGGER.info(
            "%s %s: checking %d live perpetual contracts: %s",
            self.exchange,
            self.timeframe,
            len(products),
            ", ".join(sorted(product.symbol for product in products)),
        )

        now = int(time.time())
        scans: list[ProductScan] = []
        failures = 0
        skipped = 0
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
                    LOGGER.warning(
                        "%s %s candle scan failed: %s",
                        self.exchange,
                        product.symbol,
                        exc,
                    )
                    continue
                if result is not None:
                    scans.append(result)
                else:
                    skipped += 1

        if failures:
            LOGGER.warning(
                "%s %s: %d of %d contract candle requests failed; existing alerts "
                "for those products were left unchanged",
                self.exchange,
                self.timeframe,
                failures,
                len(products),
            )

        self.last_scan_matched = any(
            matches
            for result in scans
            for matches in result.matches.values()
        )
        self.last_scan_complete = failures == 0 and skipped == 0

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
                            exchange=self.exchange,
                            timeframe=self.timeframe,
                        )
                    )

        if not new_signals:
            if not defer_notifications:
                LOGGER.info(
                        "%s %s scan complete: %d contracts checked, no new signals",
                        self.exchange,
                        self.timeframe,
                    len(scans),
                )
            return []

        if defer_notifications:
            return new_signals
        if self.dry_run:
            print("\n\n".join(message for message, _ in _format_messages(new_signals)))
            LOGGER.info("Dry run: %d new signals; no alerts or state changes", len(new_signals))
            return new_signals
        if self.telegram is None:
            raise TelegramError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to send alerts")

        messages = _format_messages(new_signals)
        for message, batch in messages:
            self.telegram.send(message)
            self.store.set_active(batch)
        LOGGER.info(
            "%s %s scan complete: %d contracts checked, sent %d new signals in %d messages",
            self.exchange,
            self.timeframe,
            len(scans),
            len(new_signals),
            len(messages),
        )
        return new_signals


class ExchangeScanError(RuntimeError):
    pass


class MultiExchangeScanner:
    def __init__(self, scanners: list[Scanner]) -> None:
        self.scanners = scanners

    def scan(self, timeframes: set[str] | None = None) -> None:
        failures: list[str] = []
        new_signals: list[Signal] = []
        selected_scanners = [
            scanner
            for scanner in self.scanners
            if timeframes is None or scanner.timeframe in timeframes
        ]
        for scanner in selected_scanners:
            try:
                new_signals.extend(scanner.scan(defer_notifications=True))
            except (MarketDataApiError, TelegramError) as exc:
                failures.append(f"{scanner.exchange} {scanner.timeframe}: {exc}")

        no_match_timeframes = [
            timeframe
            for timeframe in ("15m", "1h", "1d")
            if any(scanner.timeframe == timeframe for scanner in selected_scanners)
            and all(
                scanner.last_scan_complete
                for scanner in selected_scanners
                if scanner.timeframe == timeframe
            )
            and not any(
                scanner.last_scan_matched
                for scanner in selected_scanners
                if scanner.timeframe == timeframe
            )
        ]
        if new_signals or no_match_timeframes:
            self._send_signals(
                new_signals,
                selected_scanners,
                no_match_timeframes,
            )
        if failures:
            raise ExchangeScanError(
                "One or more exchange scans failed: " + "; ".join(failures)
            )

    def _send_signals(
        self,
        signals: list[Signal],
        scanners: list[Scanner],
        no_match_timeframes: list[str],
    ) -> None:
        messages = _format_messages(signals)
        for timeframe in no_match_timeframes:
            label = {"15m": "15M", "1h": "1HR", "1d": "1D"}[timeframe]
            messages.append((f"No matching data on {label} TF", []))
        dry_run = scanners[0].dry_run
        if dry_run:
            print("\n\n".join(message for message, _ in messages))
            LOGGER.info(
                "Dry run: %d new signals; no alerts or state changes",
                len(signals),
            )
            return

        telegram = scanners[0].telegram
        if telegram is None:
            raise TelegramError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to send alerts")

        for message, batch in messages:
            telegram.send(message)
            for scanner in scanners:
                exchange_batch = [
                    signal
                    for signal in batch
                    if signal.exchange == scanner.exchange
                    and signal.timeframe == scanner.timeframe
                ]
                if exchange_batch:
                    scanner.store.set_active(exchange_batch)
        LOGGER.info(
            "Combined scan complete: sent %d new signals in %d messages",
            len(signals),
            len(messages),
        )


class AlertStoreCollection:
    def __init__(self, stores: list[AlertStore]) -> None:
        self.stores = stores

    def close(self) -> None:
        for store in self.stores:
            store.close()


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


def create_scanner(
    dry_run: bool = False,
    timeframes: tuple[str, ...] | None = None,
) -> tuple[MultiExchangeScanner, AlertStoreCollection]:
    load_environment_file()
    telegram = None if dry_run else telegram_client_from_environment()
    selected_timeframes = timeframes or tuple(TIMEFRAME_SECONDS)
    if any(timeframe not in TIMEFRAME_SECONDS for timeframe in selected_timeframes):
        raise ValueError(f"Unsupported timeframe in {selected_timeframes}")
    delta_base_url = os.environ.get(
        "DELTA_API_BASE_URL",
        "https://api.india.delta.exchange",
    ).strip()
    bingx_base_url = os.environ.get(
        "BINGX_API_BASE_URL",
        "https://open-api.bingx.com",
    ).strip()
    if not delta_base_url.startswith("https://"):
        raise ValueError("DELTA_API_BASE_URL must use HTTPS")
    if not bingx_base_url.startswith("https://"):
        raise ValueError("BINGX_API_BASE_URL must use HTTPS")

    directory = Path(__file__).parent
    state_paths = {
        "15m": {
            "Delta Exchange": "delta_scanner_state.sqlite3",
            "BingX": "bingx_scanner_state.sqlite3",
        },
        "1h": {
            "Delta Exchange": "delta_scanner_1h_state.sqlite3",
            "BingX": "bingx_scanner_1h_state.sqlite3",
        },
        "1d": {
            "Delta Exchange": "delta_scanner_1d_state.sqlite3",
            "BingX": "bingx_scanner_1d_state.sqlite3",
        },
    }
    stores: list[AlertStore] = []
    scanners: list[Scanner] = []
    for timeframe in selected_timeframes:
        for exchange, base_url, client_type in (
            ("Delta Exchange", delta_base_url, DeltaExchangeClient),
            ("BingX", bingx_base_url, BingXClient),
        ):
            store = AlertStore(directory / state_paths[timeframe][exchange])
            stores.append(store)
            scanners.append(
                Scanner(
                    client_type(base_url, timeframe=timeframe),
                    store,
                    telegram,
                    dry_run,
                    exchange,
                    timeframe,
                )
            )
    return MultiExchangeScanner(scanners), AlertStoreCollection(stores)
