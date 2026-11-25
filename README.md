# Delta Exchange and BingX multi-timeframe RSI/MACD/EMA50 scanner

This standalone Python service scans configured Delta Exchange and BingX perpetual contracts using completed **15-minute, 1-hour, and daily candles**. It sends Telegram alerts when a contract newly meets either condition:

- RSI > 70, MACD line > MACD signal line, and candle close > EMA(50).
- RSI < 30, MACD line < MACD signal line, and candle close < EMA(50).

Bullish and bearish signals are sent in separate messages, labeled with their exchange and timeframe. It scans 15-minute candles every 15 minutes, hourly candles once per hour, and daily candles once per day. A signal is sent once while the condition remains true for that exchange, symbol, and timeframe; it can alert again if the condition first becomes false and later true again. Alert state is stored separately for each exchange and timeframe.

The scanner uses both exchanges' public market-data endpoints and does not place trades or require an API key:

- [Delta Exchange API documentation](https://docs.delta.exchange/)
- [BingX API documentation](https://bingx-api.github.io/docs/)

## Requirements

- Python 3.10 or newer.
- A Telegram bot token and the destination chat ID.
- Network access to Delta Exchange, BingX, and Telegram.

No third-party Python packages are needed.

## Telegram setup

1. Create a bot with Telegram's `@BotFather` and copy its token.
2. In Telegram, open the bot and send `/start`.
3. Copy `.env.example` to `.env` in this folder:

   ```powershell
   Copy-Item .env.example .env
   ```

4. Put the new bot token in `.env` as `TELEGRAM_BOT_TOKEN`. Set `TELEGRAM_CHAT_ID` to your destination chat ID. The `.env` file is ignored by Git and should stay private.
5. To look up chat IDs, run this command after setting the bot token in `.env`:

   ```powershell
   python main.py --telegram-chat-id
   ```

   Send `/start` to your bot first if it reports no chats.

Keep the bot token private. If it has been shared, revoke it with `@BotFather`, generate a new token, and update `.env`.

## Choose symbols

Set a comma-separated symbol list for each exchange and timeframe in `.env`:

```dotenv
DELTA_15M_SYMBOLS=BTCUSD,ETHUSD,SOLUSD,XRPUSD
DELTA_1H_SYMBOLS=BTCUSD,ETHUSD,SOLUSD,XRPUSD
DELTA_1D_SYMBOLS=BTCUSD,ETHUSD,SOLUSD,XRPUSD
BINGX_15M_SYMBOLS=BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT
BINGX_1H_SYMBOLS=BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT
BINGX_1D_SYMBOLS=BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT
```

The scanner logs the symbols checked and warns if a configured symbol is not an active perpetual contract. If a timeframe's list is empty, that exchange scans all its live perpetual contracts for that timeframe. Legacy `DELTA_SYMBOLS` and `BINGX_SYMBOLS` values are used as fallbacks for any timeframe-specific list that is unset.

## Run

Run continuously, scanning each timeframe at its own cadence:

```powershell
python main.py
```

## GitHub Actions automation

The `Delta scanner` workflow runs 15-minute scans every quarter-hour, starting at 05:30 IST (00:00 UTC); hourly scans at :30 IST; and daily scans at 05:30 IST (00:00 UTC). Add `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID` as repository secrets under **Settings → Secrets and variables
→ Actions**. You can also start a scan manually from the repository's **Actions** tab.

Run all timeframes once:

```powershell
python main.py --once
```

Run one timeframe:

```powershell
python main.py --once --timeframe 15m
python main.py --once --timeframe 1h
python main.py --once --timeframe 1d
```

Preview signals without Telegram or changing the saved alert state:

```powershell
python main.py --once --dry-run
```

Run the local tests:

```powershell
python -m unittest -v
```

## Notes

- The scanner covers crypto perpetual futures, not NSE-listed stocks. Product availability comes from both exchanges' APIs.
- It uses Wilder's 14-period RSI, standard 12/26/9 MACD, and 50-period EMA calculated from candle closes.
- A 120-candle history is requested per product and timeframe. Products with fewer than 50 completed candles are skipped with a warning.
- Missing/failed candle requests are logged; alert state for those products is retained rather than reset.
- This is a technical-indicator alert, not investment advice or an instruction to trade.
