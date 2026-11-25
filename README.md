# BingX 15-minute RSI/MACD/EMA50 scanner

This standalone Python service scans live BingX perpetual contracts, excluding options. It evaluates **completed 15-minute candles** and sends one Telegram message when a contract newly meets either condition:

- RSI > 70, MACD line > MACD signal line, and candle close > EMA(50).
- RSI < 30, MACD line < MACD signal line, and candle close < EMA(50).

It polls every 15 minutes, 24/7. A signal is sent once while the condition remains true; it can alert again if the condition first becomes false and later true again. Alert state is stored in `bingx_scanner_state.sqlite3` beside the script.

The scanner uses BingX's public market-data endpoints and does not place trades or require an API key:

- [BingX API documentation](https://bingx-api.github.io/docs/)
- [Perpetual contracts](https://open-api.bingx.com/openApi/swap/v2/quote/contracts)
- [Perpetual candlesticks](https://open-api.bingx.com/openApi/swap/v2/quote/klines)

## Requirements

- Python 3.10 or newer.
- A Telegram bot token and the destination chat ID.
- Network access to BingX and Telegram.

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

## Run

Run continuously, scanning on each 15-minute boundary:

```powershell
python main.py
```

Run a single scan:

```powershell
python main.py --once
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

- The scanner covers BingX-listed perpetual contracts, not NSE-listed stocks. Product availability and symbols come from BingX's API.
- It uses Wilder's 14-period RSI, standard 12/26/9 MACD, and 50-period EMA calculated from candle closes.
- A 120-candle history is requested per product. Products with fewer than 50 completed candles are skipped with a warning.
- Missing/failed candle requests are logged; alert state for those products is retained rather than reset.
- This is a technical-indicator alert, not investment advice or an instruction to trade.
