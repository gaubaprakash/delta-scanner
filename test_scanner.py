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
    MIN_CANDLES,
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

        self.assertEqual(
            client.live_products(),
            [Product(symbol="BTC-USDT", contract_type="perpetual")],
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
                    "BingX 15-minute signal (completed candle)\n\n"
                )
                for message, _ in messages
            )
        )
        self.assertEqual(sum(len(batch) for _, batch in messages), len(signals))


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
