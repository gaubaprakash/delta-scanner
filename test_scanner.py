from __future__ import annotations

from io import BytesIO
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from indicators import Indicators, calculate_indicators, matching_setups
from main import ISTFormatter
from scanner import (
    AlertStore,
    BinanceUsdMFuturesClient,
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
    _configured_symbols,
    _format_messages,
    _google_sheet_symbol_config,
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
    def test_api_requests_identify_the_scanner_user_agent(self) -> None:
        client = BingXClient("https://example.invalid")
        response = BytesIO(b'{"code":0,"data":[]}')
        with patch("scanner.urlopen", return_value=response) as urlopen_mock:
            self.assertEqual(client._get_json("/contracts", {}), {"code": 0, "data": []})

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "delta-scanner/1.0")

    def test_only_completed_candles_are_used(self) -> None:
        client = BingXClient("https://example.invalid")
        client._get_json = Mock(
            return_value={
                "code": 0,
                "data": [
                    {"time": 900_000, "close": "10"},
                    {"time": 1_800_000, "close": "11"},
                    {"time": 2_700_000, "close": "12"},
                ],
            }
        )

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
        client._get_json = Mock(
            return_value={
                "code": 0,
                "data": [
                    {"time": 0, "close": "10"},
                    {"time": 86_400_000, "close": "11"},
                ],
            }
        )

        self.assertEqual(client.candles("BTC-USDT", now=172799), [(0, 10.0)])

    def test_only_active_bingx_perpetual_contracts_are_included(self) -> None:
        client = BingXClient("https://example.invalid")
        client._get_json = Mock(
            return_value={
                "code": 0,
                "data": [
                    {"symbol": "BTC-USDT", "status": 1},
                    {"symbol": "OLD-USDT", "status": 0},
                    {"symbol": "", "status": 1},
                ],
            }
        )

        with patch(
            "scanner._configured_symbols",
            return_value=set(),
        ):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="BTC-USDT", contract_type="perpetual")],
            )

    def test_only_configured_bingx_symbols_are_included(self) -> None:
        client = BingXClient("https://example.invalid")
        client._get_json = Mock(
            return_value={
                "code": 0,
                "data": [
                    {"symbol": "BTC-USDT", "status": 1},
                    {"symbol": "ETH-USDT", "status": 1},
                    {"symbol": "OLD-USDT", "status": 0},
                ],
            }
        )

        with patch(
            "scanner._configured_symbols",
            return_value={"BTC-USDT", "NOT-LIVE"},
        ):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="BTC-USDT", contract_type="perpetual")],
            )

    def test_hourly_bingx_symbol_list_is_independent(self) -> None:
        client = BingXClient("https://example.invalid", timeframe="1h")
        client._get_json = Mock(
            return_value={
                "code": 0,
                "data": [
                    {"symbol": "BTC-USDT", "status": 1},
                    {"symbol": "ETH-USDT", "status": 1},
                ],
            }
        )

        with patch(
            "scanner._configured_symbols",
            return_value={"ETH-USDT"},
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
        client._get_json = Mock(
            return_value={
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
        )

        with patch(
            "scanner._configured_symbols",
            return_value={"BTCUSD", "NOTLIVE"},
        ):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="BTCUSD", contract_type="perpetual")],
            )

    def test_hourly_delta_symbol_list_is_independent(self) -> None:
        client = DeltaExchangeClient("https://example.invalid", timeframe="1h")
        client._get_json = Mock(
            return_value={
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
        )

        with patch(
            "scanner._configured_symbols",
            return_value={"ETHUSD"},
        ):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="ETHUSD", contract_type="perpetual")],
            )


class BinanceUsdMFuturesClientTests(unittest.TestCase):
    def test_only_configured_active_usdt_perpetuals_are_included(self) -> None:
        client = BinanceUsdMFuturesClient("https://example.invalid")
        client._get_json = Mock(
            return_value={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    },
                    {
                        "symbol": "ETHUSDT",
                        "status": "TRADING",
                        "contractType": "CURRENT_QUARTER",
                        "quoteAsset": "USDT",
                    },
                    {
                        "symbol": "SOLUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    },
                ]
            }
        )

        with patch(
            "scanner._configured_symbols",
            return_value={"BTC-USDT", "ETH-USDT"},
        ):
            self.assertEqual(
                client.live_products(),
                [Product(symbol="BTCUSDT", contract_type="perpetual")],
            )

    def test_requests_and_filters_completed_klines(self) -> None:
        client = BinanceUsdMFuturesClient("https://example.invalid")
        client._get_json = Mock(
            return_value=[
                [0, "1", "1", "1", "10", "0", 899999],
                [900000, "1", "1", "1", "11", "0", 1799999],
                [1800000, "1", "1", "1", "12", "0", 2699999],
            ]
        )

        self.assertEqual(
            client.candles("BTCUSDT", now=1800),
            [(0, 10.0), (900, 11.0)],
        )
        client._get_json.assert_called_once_with(
            "/fapi/v1/klines",
            {"symbol": "BTCUSDT", "interval": "15m", "limit": 120},
        )


class GoogleSheetConfigurationTests(unittest.TestCase):
    def test_binance_database_rows_parse_only_when_active(self) -> None:
        sheet_csv = (
            "Record_ID,Exchange,Timeframe,Symbol,Is_Active,Source_Key\n"
            "SYM-058,BINANCE,15M,BTC-USDT,TRUE,BINANCE_15M_SYMBOLS\n"
            "SYM-059,BINANCE,15M,ETH-USDT,FALSE,BINANCE_15M_SYMBOLS\n"
        )
        with patch(
            "scanner._sheet_symbol_cache",
            {},
        ), patch.dict(
            "os.environ",
            {"GOOGLE_SHEET_CSV_URL": "https://example.invalid/sheet.csv"},
        ), patch(
            "scanner.urlopen",
            return_value=BytesIO(sheet_csv.encode("utf-8")),
        ):
            self.assertEqual(
                _google_sheet_symbol_config(),
                {"BINANCE_15M_SYMBOLS": {"BTC-USDT"}},
            )

    def test_missing_sheet_value_fail_closes_instead_of_using_env_or_all_symbols(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_SHEET_CSV_URL": "https://example.invalid/sheet.csv",
                "DELTA_1H_SYMBOLS": "BTCUSD",
            },
        ), patch(
            "scanner._google_sheet_symbol_config",
            return_value={},
        ):
            self.assertIsNone(
                _configured_symbols("delta", "1h", "DELTA_SYMBOLS")
            )

    def test_sheet_config_overrides_matching_environment_fallback(self) -> None:
        sheet_config = {"DELTA_1H_SYMBOLS": {"ETHUSD", "SOLUSD"}}
        with patch.dict(
            "os.environ",
            {"DELTA_1H_SYMBOLS": "BTCUSD"},
        ), patch(
            "scanner._google_sheet_symbol_config",
            return_value=sheet_config,
        ):
            self.assertEqual(
                _configured_symbols("delta", "1h", "DELTA_SYMBOLS"),
                {"ETHUSD", "SOLUSD"},
            )

    def test_binance_symbols_are_mapped_from_sheet_bingx_usdt_pairs(self) -> None:
        sheet_config = {
            "BINGX_15M_SYMBOLS": {
                "BTC-USDT",
                "ETH-USDT",
                "NIFTY 50",
                "OIL BRENT",
            }
        }
        with patch(
            "scanner._google_sheet_symbol_config",
            return_value=sheet_config,
        ):
            self.assertEqual(
                _configured_symbols("binance", "15m", "BINANCE_SYMBOLS"),
                {"BTCUSDT", "ETHUSDT"},
            )

    def test_explicit_binance_sheet_symbols_override_bingx_mapping(self) -> None:
        sheet_config = {
            "BINANCE_1H_SYMBOLS": {"SOLUSDT"},
            "BINGX_1H_SYMBOLS": {"BTC-USDT"},
        }
        with patch(
            "scanner._google_sheet_symbol_config",
            return_value=sheet_config,
        ):
            self.assertEqual(
                _configured_symbols("binance", "1h", "BINANCE_SYMBOLS"),
                {"SOLUSDT"},
            )

    def test_legacy_environment_symbols_are_not_used(self) -> None:
        with patch.dict(
            "os.environ",
            {"DELTA_1H_SYMBOLS": "ETHUSD,SOLUSD"},
        ), patch(
            "scanner._google_sheet_symbol_config",
            return_value={},
        ):
            self.assertIsNone(
                _configured_symbols("delta", "1h", "DELTA_SYMBOLS")
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
    def test_telegram_send_logs_api_confirmed_chat_and_message_ids(self) -> None:
        client = TelegramClient("token", "-123")
        with patch.object(
            client,
            "_request",
            return_value={
                "ok": True,
                "result": {
                    "message_id": 456,
                    "chat": {"id": -123},
                },
            },
        ) as request:
            with self.assertLogs("bingx_scanner", level="INFO") as logs:
                client.send("test")

        request.assert_called_once_with(
            "sendMessage",
            {
                "chat_id": "-123",
                "text": "test",
                "disable_web_page_preview": True,
            },
        )
        self.assertIn("message_id=456 for chat_id=-123", logs.output[0])

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

    def test_log_timestamps_are_formatted_in_ist(self) -> None:
        record = logging.LogRecord("test", logging.INFO, "", 0, "test", (), None)
        record.created = 0
        record.msecs = 0

        self.assertEqual(
            ISTFormatter("%(asctime)s").formatTime(record),
            "1970-01-01 05:30:00,000",
        )

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
                    "Delta Exchange | 15-minute signals (completed candle)\n\n"
                )
                for message, _ in messages
            )
        )
        self.assertEqual(sum(len(batch) for _, batch in messages), len(signals))

    def test_messages_are_grouped_by_exchange_and_timeframe(self) -> None:
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
        self.assertTrue(
            messages[0][0].startswith(
                "Delta Exchange | 15-minute signals (completed candle)"
            )
        )
        self.assertIn("BULLUSD: RSI > 70 and bullish MACD", messages[0][0])
        self.assertIn("BEARUSD: RSI < 30 and bearish MACD", messages[0][0])
        self.assertIn("Candle: 1970-01-01 05:45 IST", messages[0][0])
        self.assertTrue(
            messages[1][0].startswith(
                "BingX | 15-minute signals (completed candle)"
            )
        )
        self.assertIn("BINGX-BULL: RSI > 70 and bullish MACD", messages[1][0])
        self.assertNotIn("BEARUSD", messages[1][0])

    def test_signals_are_grouped_by_exchange_and_timeframe(self) -> None:
        signals = [
            Signal(
                Product("BTCUSD", "perpetual"),
                "overbought_bullish",
                900,
                Indicators(75, 2, 1, 101, 100),
                timeframe="15m",
            ),
            Signal(
                Product("XRPUSDT", "perpetual"),
                "oversold_bearish",
                900,
                Indicators(25, -2, -1, 99, 100),
                exchange="Binance USD-M Futures",
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

        self.assertEqual(len(messages), 4)
        self.assertTrue(messages[0][0].startswith("Delta Exchange | 15-minute"))
        self.assertTrue(messages[1][0].startswith("Binance USD-M Futures | 15-minute"))
        self.assertIn("XRPUSDT:", messages[1][0])
        self.assertTrue(messages[2][0].startswith("BingX | 1-hour"))
        self.assertIn("ETH-USDT:", messages[2][0])
        self.assertTrue(messages[3][0].startswith("Delta Exchange | daily"))


class MultiExchangeScannerTests(unittest.TestCase):
    def test_scans_every_exchange_even_if_one_fails(self) -> None:
        called: list[str] = []

        class FakeScanner:
            def __init__(self, exchange: str, should_fail: bool) -> None:
                self.exchange = exchange
                self.timeframe = "15m"
                self.should_fail = should_fail
                self.last_scan_complete = False
                self.last_scan_matched = False

            def scan(self, defer_notifications: bool = False) -> list[Signal]:
                called.append(self.exchange)
                if not defer_notifications:
                    raise AssertionError("signals should be collected before sending")
                if self.should_fail:
                    raise MarketDataApiError("unavailable")
                self.last_scan_complete = True
                self.last_scan_matched = True
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

    def test_sends_exchange_and_timeframe_grouped_messages(self) -> None:
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
                self.last_scan_complete = False
                self.last_scan_matched = False

            def scan(self, defer_notifications: bool = False) -> list[Signal]:
                if not defer_notifications:
                    raise AssertionError("signals should be collected before sending")
                self.last_scan_complete = True
                self.last_scan_matched = True
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
        self.assertTrue(telegram.messages[0].startswith("Delta Exchange | 15-minute"))
        self.assertIn("BTCUSD:", telegram.messages[0])
        self.assertIn("bullish", telegram.messages[0])
        self.assertTrue(telegram.messages[1].startswith("BingX | 15-minute"))
        self.assertIn("ETH-USDT:", telegram.messages[1])
        self.assertIn("bearish", telegram.messages[1])
        self.assertEqual(scanners[0].store.active, [delta_signal])
        self.assertEqual(scanners[1].store.active, [bingx_signal])

    def test_sends_no_match_notices_per_exchange_and_timeframe(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def send(self, message: str) -> None:
                self.messages.append(message)

        class FakeScanner:
            def __init__(self, exchange: str, timeframe: str, telegram: FakeTelegram) -> None:
                self.exchange = exchange
                self.timeframe = timeframe
                self.telegram = telegram
                self.dry_run = False
                self.store = None
                self.last_scan_complete = False
                self.last_scan_matched = False

            def scan(self, defer_notifications: bool = False) -> list[Signal]:
                if not defer_notifications:
                    raise AssertionError("signals should be collected before sending")
                self.last_scan_complete = True
                return []

        telegram = FakeTelegram()
        scanners = [
            FakeScanner(exchange, timeframe, telegram)
            for timeframe in ("15m", "1h", "1d")
            for exchange in ("Delta Exchange", "BingX")
        ]

        MultiExchangeScanner(scanners).scan()

        self.assertEqual(
            telegram.messages,
            [
                "Delta Exchange | 15-minute: No matching signals for configured symbols.",
                "BingX | 15-minute: No matching signals for configured symbols.",
                "Delta Exchange | 1-hour: No matching signals for configured symbols.",
                "BingX | 1-hour: No matching signals for configured symbols.",
                "Delta Exchange | daily: No matching signals for configured symbols.",
                "BingX | daily: No matching signals for configured symbols.",
            ],
        )


class ScannerTests(unittest.TestCase):
    def test_short_candle_history_logs_product_and_timeframe(self) -> None:
        class FakeMarketData:
            def live_products(self) -> list[Product]:
                return [Product("THINUSD", "perpetual")]

            def candles(self, symbol: str, now: int) -> list[tuple[int, float]]:
                if symbol != "THINUSD" or now <= 0:
                    raise AssertionError("Unexpected product or candle request time")
                return [(index * 900, 100.0) for index in range(MIN_CANDLES - 1)]

        with tempfile.TemporaryDirectory() as directory:
            store = AlertStore(Path(directory) / "state.sqlite3")
            scanner = Scanner(
                FakeMarketData(),
                store,
                None,
                dry_run=True,
                exchange="Delta Exchange",
                timeframe="15m",
            )

            with self.assertLogs("bingx_scanner", level="WARNING") as captured:
                self.assertEqual(scanner.scan(defer_notifications=True), [])

            self.assertIn("Delta Exchange 15m THINUSD skipped", captured.output[0])
            store.close()

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
                self.assert_scan_matches_requested_product(product, now)
                return self.result

            def assert_scan_matches_requested_product(
                self,
                product: Product,
                now: int,
            ) -> None:
                if product != self.result.product or now <= 0:
                    raise AssertionError("Unexpected product or scan time")

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
