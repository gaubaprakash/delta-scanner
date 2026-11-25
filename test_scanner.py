from __future__ import annotations

from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from indicators import Indicators, calculate_indicators, matching_setups
from scanner import (
    AlertStore,
    BingXClient,
    DeltaExchangeClient,
    ExchangeScanError,
    MarketDataApiError,
    MIN_CANDLES,
    MultiExchangeScanner,
    Product,
    ProductScan,
    Scanner,
    Signal,
    TelegramClient,
    TelegramError,
    _format_messages,
    load_environment_file,
)


class IndicatorTests(unittest.TestCase):
    def test_constant_prices_have_neutral_rsi_and_flat_macd(self) -> None:
        result = calculate_indicators([100.0] * 60)
        self.assertEqual(result.rsi, 50.0)
        self.assertEqual(result.macd, 0.0)
        self.assertEqual(result.macd_signal, 0.0)
        self.assertEqual(result.ema50, 100.0)
        self.assertEqual(result.close, 100.0)

    def test_rsi_handles_uninterrupted_moves(self) -> None:
        rising = calculate_indicators([float(value) for value in range(1, 61)])
        falling = calculate_indicators([float(value) for value in range(61, 1, -1)])
        self.assertEqual(rising.rsi, 100.0)
        self.assertEqual(falling.rsi, 0.0)

    def test_indicator_calculation_requires_enough_positive_closes(self) -> None:
        with self.assertRaises(ValueError):
            calculate_indicators([100.0] * (MIN_CANDLES - 1))
        with self.assertRaises(ValueError):
            calculate_indicators([100.0] * 49 + [0.0])

    def test_signal_combinations_are_independent_and_strict(self) -> None:
        overbought = Indicators(rsi=71, macd=2, macd_signal=1, close=101, ema50=100)
        oversold = Indicators(rsi=29, macd=-2, macd_signal=-1, close=99, ema50=100)
        boundary = Indicators(rsi=70, macd=2, macd_signal=1, close=101, ema50=100)
        below_ema = Indicators(rsi=71, macd=2, macd_signal=1, close=99, ema50=100)
        above_ema = Indicators(rsi=29, macd=-2, macd_signal=-1, close=101, ema50=100)
        on_ema = Indicators(rsi=71, macd=2, macd_signal=1, close=100, ema50=100)

        self.assertEqual(
            matching_setups(overbought),
            {"overbought_bullish": True, "oversold_bearish": False},
        )
        self.assertEqual(
            matching_setups(oversold),
            {"overbought_bullish": False, "oversold_bearish": True},
        )
        self.assertFalse(matching_setups(boundary)["overbought_bullish"])
        self.assertFalse(matching_setups(below_ema)["overbought_bullish"])
        self.assertFalse(matching_setups(above_ema)["oversold_bearish"])
        self.assertFalse(matching_setups(on_ema)["overbought_bullish"])


class BingXClientTests(unittest.TestCase):
    def test_only_completed_candles_are_used(self) -> None:
        client = BingXClient("https://example.invalid")
        client._get_json = lambda path, params: {
            "code": 0,
            "data": [
                {"time": 900_000, "close": "10"},
                {"time": 1_800_000, "close": "11"},
                {"time": 2_700_000, "close": "12"},
            ],
        }

        self.assertEqual(
            client.candles("BTC-USDT", now=3599),
            [(900, 10.0), (1800, 11.0)],
        )

    def test_requests_bingx_15_minute_candles(self) -> None:
        client = BingXClient("https://example.invalid")
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_get_json(path: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((path, params))
            return {"code": 0, "data": []}

        client._get_json = fake_get_json
        client.candles("BTC-USDT", now=10_000)

        self.assertEqual(
            calls,
            [
                (
                    "/openApi/swap/v2/quote/klines",
                    {"symbol": "BTC-USDT", "interval": "15m", "limit": 120},
                )
            ],
        )

    def test_requests_completed_one_hour_candles(self) -> None:
        client = BingXClient("https://example.invalid", timeframe="1h")
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_get_json(path: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((path, params))
            return {
                "code": 0,
                "data": [
                    {"time": 0, "close": "10"},
                    {"time": 3_600_000, "close": "11"},
                    {"time": 7_200_000, "close": "12"},
                ],
            }

        client._get_json = fake_get_json

        self.assertEqual(client.candles("BTC-USDT", now=7199), [(0, 10.0)])
        self.assertEqual(
            calls[0][1],
            {"symbol": "BTC-USDT", "interval": "1h", "limit": 120},
        )

    def test_requests_completed_daily_candles(self) -> None:
        client = BingXClient("https://example.invalid", timeframe="1d")
        client._get_json = lambda path, params: {
            "code": 0,
            "data": [
                {"time": 0, "close": "10"},
                {"time": 86_400_000, "close": "11"},
            ],
        }

        self.assertEqual(client.candles("BTC-USDT", now=172799), [(0, 10.0)])

    def test_only_active_bingx_perpetual_contracts_are_included(self) -> None:
        client = BingXClient("https://example.invalid")
        client._get_json = lambda path, params: {
            "code": 0,
            "data": [
                {"symbol": "BTC-USDT", "status": 1},
                {"symbol": "OLD-USDT", "status": 0},
                {"symbol": "", "status": 1},
            ],
        }

        with patch.dict("os.environ", {"BINGX_SYMBOLS": ""}):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="BTC-USDT", contract_type="perpetual")],
            )

    def test_only_configured_bingx_symbols_are_included(self) -> None:
        client = BingXClient("https://example.invalid")
        client._get_json = lambda path, params: {
            "code": 0,
            "data": [
                {"symbol": "BTC-USDT", "status": 1},
                {"symbol": "ETH-USDT", "status": 1},
                {"symbol": "OLD-USDT", "status": 0},
            ],
        }

        with patch.dict("os.environ", {"BINGX_SYMBOLS": "btc-usdt,NOT-LIVE"}):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="BTC-USDT", contract_type="perpetual")],
            )

    def test_hourly_bingx_symbol_list_is_independent(self) -> None:
        client = BingXClient("https://example.invalid", timeframe="1h")
        client._get_json = lambda path, params: {
            "code": 0,
            "data": [
                {"symbol": "BTC-USDT", "status": 1},
                {"symbol": "ETH-USDT", "status": 1},
            ],
        }

        with patch.dict(
            "os.environ",
            {
                "BINGX_SYMBOLS": "BTC-USDT",
                "BINGX_1H_SYMBOLS": "ETH-USDT",
            },
        ):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="ETH-USDT", contract_type="perpetual")],
            )


class DeltaExchangeClientTests(unittest.TestCase):
    def test_requests_one_hour_candles_and_history(self) -> None:
        client = DeltaExchangeClient("https://example.invalid", timeframe="1h")
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_get_json(path: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((path, params))
            return {"success": True, "result": []}

        client._get_json = fake_get_json
        client.candles("BTCUSD", now=200_000)

        self.assertEqual(
            calls,
            [
                (
                    "/v2/history/candles",
                    {
                        "resolution": "1h",
                        "symbol": "BTCUSD",
                        "start": 200_000 - (120 * 3600),
                        "end": 200_000,
                    },
                )
            ],
        )

    def test_requests_daily_candles_and_history(self) -> None:
        client = DeltaExchangeClient("https://example.invalid", timeframe="1d")
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_get_json(path: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((path, params))
            return {"success": True, "result": []}

        client._get_json = fake_get_json
        client.candles("BTCUSD", now=2_000_000)

        self.assertEqual(
            calls[0][1],
            {
                "resolution": "1d",
                "symbol": "BTCUSD",
                "start": 2_000_000 - (120 * 86_400),
                "end": 2_000_000,
            },
        )

    def test_only_configured_live_perpetuals_are_included(self) -> None:
        client = DeltaExchangeClient("https://example.invalid")
        client._get_json = lambda path, params: {
            "success": True,
            "result": [
                {
                    "symbol": "BTCUSD",
                    "contract_type": "perpetual_futures",
                    "state": "live",
                    "trading_status": "operational",
                },
                {
                    "symbol": "ETHUSD",
                    "contract_type": "perpetual_futures",
                    "state": "live",
                    "trading_status": "operational",
                },
                {
                    "symbol": "OLDUSD",
                    "contract_type": "perpetual_futures",
                    "state": "expired",
                    "trading_status": "operational",
                },
            ],
        }

        with patch.dict("os.environ", {"DELTA_SYMBOLS": "BTCUSD,NOTLIVE"}):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="BTCUSD", contract_type="perpetual")],
            )

    def test_hourly_delta_symbol_list_is_independent(self) -> None:
        client = DeltaExchangeClient("https://example.invalid", timeframe="1h")
        client._get_json = lambda path, params: {
            "success": True,
            "result": [
                {
                    "symbol": "BTCUSD",
                    "contract_type": "perpetual_futures",
                    "state": "live",
                    "trading_status": "operational",
                },
                {
                    "symbol": "ETHUSD",
                    "contract_type": "perpetual_futures",
                    "state": "live",
                    "trading_status": "operational",
                },
            ],
        }

        with patch.dict(
            "os.environ",
            {"DELTA_SYMBOLS": "BTCUSD", "DELTA_1H_SYMBOLS": "ETHUSD"},
        ):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="ETHUSD", contract_type="perpetual")],
            )


class EnvironmentFileTests(unittest.TestCase):
    def test_loads_values_without_overriding_existing_environment(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# scanner secrets\n"
                "SCANNER_TEST_VALUE='from-file'\n"
                "SCANNER_EXISTING_VALUE=file-value\n",
                encoding="utf-8",
            )
            previous_test_value = os.environ.get("SCANNER_TEST_VALUE")
            previous_existing_value = os.environ.get("SCANNER_EXISTING_VALUE")
            os.environ["SCANNER_EXISTING_VALUE"] = "from-environment"
            os.environ.pop("SCANNER_TEST_VALUE", None)
            try:
                load_environment_file(path)
                self.assertEqual(os.environ["SCANNER_TEST_VALUE"], "from-file")
                self.assertEqual(
                    os.environ["SCANNER_EXISTING_VALUE"],
                    "from-environment",
                )
            finally:
                if previous_test_value is None:
                    os.environ.pop("SCANNER_TEST_VALUE", None)
                else:
                    os.environ["SCANNER_TEST_VALUE"] = previous_test_value
                if previous_existing_value is None:
                    os.environ.pop("SCANNER_EXISTING_VALUE", None)
                else:
                    os.environ["SCANNER_EXISTING_VALUE"] = previous_existing_value

    def test_rejects_malformed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("not-an-assignment\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_environment_file(path)


class AlertStoreTests(unittest.TestCase):
    def test_telegram_http_errors_include_api_description(self) -> None:
        error = HTTPError(
            "https://api.telegram.org/botTOKEN/sendMessage",
            400,
            "Bad Request",
            None,
            BytesIO(b'{"ok":false,"description":"Bad Request: chat not found"}'),
        )
        with patch("scanner.urlopen", side_effect=error):
            with self.assertRaisesRegex(TelegramError, "chat not found"):
                TelegramClient("token", "chat").send("test")

    def test_signal_state_persists_and_can_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = AlertStore(path)
            self.assertFalse(store.is_active("BTC-USDT", "overbought_bullish"))

            signal = Signal(
                product=Product("BTC-USDT", "perpetual"),
                setup="overbought_bullish",
                candle_time=900,
                indicators=Indicators(75, 2, 1, 101, 100),
            )
            store.set_active([signal])
            self.assertTrue(store.is_active("BTC-USDT", "overbought_bullish"))
            store.close()

            reopened = AlertStore(path)
            self.assertTrue(reopened.is_active("BTC-USDT", "overbought_bullish"))
            reopened.set_inactive("BTC-USDT", "overbought_bullish")
            self.assertFalse(reopened.is_active("BTC-USDT", "overbought_bullish"))
            reopened.close()

    def test_telegram_messages_stay_below_limit_and_keep_signal_batches(self) -> None:
        signals = [
            Signal(
                product=Product(f"PRODUCT{index}", "perpetual"),
                setup="overbought_bullish",
                candle_time=900,
                indicators=Indicators(75, 2, 1, 101, 100),
            )
            for index in range(40)
        ]

        messages = _format_messages(signals)

        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 3500 for message, _ in messages))
        self.assertTrue(
            all(
                message.startswith(
                    "Crypto 15-minute bullish signals (completed candle)\n\n"
                )
                for message, _ in messages
            )
        )
        self.assertEqual(sum(len(batch) for _, batch in messages), len(signals))

    def test_bullish_and_bearish_signals_are_separate_messages(self) -> None:
        signals = [
            Signal(
                product=Product("BULLUSD", "perpetual"),
                setup="overbought_bullish",
                candle_time=900,
                indicators=Indicators(75, 2, 1, 101, 100),
            ),
            Signal(
                product=Product("BEARUSD", "perpetual"),
                setup="oversold_bearish",
                candle_time=900,
                indicators=Indicators(25, -2, -1, 99, 100),
            ),
            Signal(
                product=Product("BINGX-BULL", "perpetual"),
                setup="overbought_bullish",
                candle_time=900,
                indicators=Indicators(75, 2, 1, 101, 100),
                exchange="BingX",
            ),
        ]

        messages = _format_messages(signals)

        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0][0].startswith("Crypto 15-minute bullish"))
        self.assertIn("bullish signals", messages[0][0])
        self.assertIn("Delta Exchange | BULLUSD", messages[0][0])
        self.assertIn("BingX | BINGX-BULL", messages[0][0])
        self.assertNotIn("BEARUSD", messages[0][0])
        self.assertTrue(messages[1][0].startswith("Crypto 15-minute bearish"))
        self.assertIn("bearish signals", messages[1][0])
        self.assertIn("Delta Exchange | BEARUSD", messages[1][0])

    def test_signals_are_grouped_by_timeframe(self) -> None:
        signals = [
            Signal(
                Product("BTCUSD", "perpetual"),
                "overbought_bullish",
                900,
                Indicators(75, 2, 1, 101, 100),
                timeframe="15m",
            ),
            Signal(
                Product("ETH-USDT", "perpetual"),
                "overbought_bullish",
                3600,
                Indicators(75, 2, 1, 101, 100),
                exchange="BingX",
                timeframe="1h",
            ),
            Signal(
                Product("SOLUSD", "perpetual"),
                "oversold_bearish",
                86400,
                Indicators(25, -2, -1, 99, 100),
                timeframe="1d",
            ),
        ]

        messages = _format_messages(signals)

        self.assertEqual(len(messages), 3)
        self.assertIn("15-minute bullish", messages[0][0])
        self.assertIn("1-hour bullish", messages[1][0])
        self.assertIn("BingX | ETH-USDT", messages[1][0])
        self.assertIn("daily bearish", messages[2][0])


class MultiExchangeScannerTests(unittest.TestCase):
    def test_scans_every_exchange_even_if_one_fails(self) -> None:
        called: list[str] = []

        class FakeScanner:
            def __init__(self, exchange: str, should_fail: bool) -> None:
                self.exchange = exchange
                self.timeframe = "15m"
                self.should_fail = should_fail

            def scan(self, defer_notifications: bool = False) -> list[Signal]:
                called.append(self.exchange)
                if self.should_fail:
                    raise MarketDataApiError("unavailable")
                return []

        scanner = MultiExchangeScanner(
            [FakeScanner("Delta Exchange", True), FakeScanner("BingX", False)]
        )

        with self.assertRaisesRegex(
            ExchangeScanError,
            "Delta Exchange 15m: unavailable",
        ):
            scanner.scan()

        self.assertEqual(called, ["Delta Exchange", "BingX"])

    def test_sends_combined_bullish_and_bearish_messages_with_exchange_labels(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def send(self, message: str) -> None:
                self.messages.append(message)

        class FakeStore:
            def __init__(self) -> None:
                self.active: list[Signal] = []

            def set_active(self, signals: list[Signal]) -> None:
                self.active.extend(signals)

        class FakeScanner:
            def __init__(
                self,
                exchange: str,
                telegram: FakeTelegram,
                signal: Signal,
            ) -> None:
                self.exchange = exchange
                self.timeframe = signal.timeframe
                self.telegram = telegram
                self.dry_run = False
                self.store = FakeStore()
                self.signal = signal

            def scan(self, defer_notifications: bool = False) -> list[Signal]:
                return [self.signal]

        telegram = FakeTelegram()
        delta_signal = Signal(
            Product("BTCUSD", "perpetual"),
            "overbought_bullish",
            900,
            Indicators(75, 2, 1, 101, 100),
            "Delta Exchange",
        )
        bingx_signal = Signal(
            Product("ETH-USDT", "perpetual"),
            "oversold_bearish",
            900,
            Indicators(25, -2, -1, 99, 100),
            "BingX",
        )
        scanners = [
            FakeScanner("Delta Exchange", telegram, delta_signal),
            FakeScanner("BingX", telegram, bingx_signal),
        ]

        MultiExchangeScanner(scanners).scan()

        self.assertEqual(len(telegram.messages), 2)
        self.assertIn("Delta Exchange | BTCUSD", telegram.messages[0])
        self.assertIn("bullish", telegram.messages[0])
        self.assertIn("BingX | ETH-USDT", telegram.messages[1])
        self.assertIn("bearish", telegram.messages[1])
        self.assertEqual(scanners[0].store.active, [delta_signal])
        self.assertEqual(scanners[1].store.active, [bingx_signal])


class ScannerTests(unittest.TestCase):
    def test_alert_is_once_per_qualifying_episode(self) -> None:
        class FakeBingX:
            def live_products(self) -> list[Product]:
                return [Product("BTC-USDT", "perpetual")]

        class FakeTelegram:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def send(self, message: str) -> None:
                self.messages.append(message)

        class FakeScanner(Scanner):
            result: ProductScan

            def _scan_product(self, product: Product, now: int) -> ProductScan:
                return self.result

        with tempfile.TemporaryDirectory() as directory:
            store = AlertStore(Path(directory) / "state.sqlite3")
            telegram = FakeTelegram()
            scanner = FakeScanner(FakeBingX(), store, telegram)
            product = Product("BTC-USDT", "perpetual")
            indicators = Indicators(75, 2, 1, 101, 100)

            scanner.result = ProductScan(
                product, 900, indicators,
                {"overbought_bullish": True, "oversold_bearish": False},
            )
            scanner.scan()
            scanner.scan()
            self.assertEqual(len(telegram.messages), 1)

            scanner.result = ProductScan(
                product, 1800, indicators,
                {"overbought_bullish": False, "oversold_bearish": False},
            )
            scanner.scan()
            self.assertEqual(len(telegram.messages), 1)

            scanner.result = ProductScan(
                product, 2700, indicators,
                {"overbought_bullish": True, "oversold_bearish": False},
            )
            scanner.scan()
            self.assertEqual(len(telegram.messages), 2)
            store.close()


if __name__ == "__main__":
    unittest.main()
