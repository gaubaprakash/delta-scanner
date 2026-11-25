from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Indicators:
    rsi: float
    macd: float
    macd_signal: float
    close: float
    ema50: float


def _ema(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2 / (period + 1)

    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current

    return result


def calculate_indicators(closes: Sequence[float]) -> Indicators:
    if len(closes) < 50:
        raise ValueError("At least 50 closed candles are required")
    if any(value <= 0 for value in closes):
        raise ValueError("Closing prices must be positive")

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(closes, closes[1:]):
        difference = current - previous
        gains.append(max(difference, 0.0))
        losses.append(max(-difference, 0.0))

    average_gain = sum(gains[:14]) / 14
    average_loss = sum(losses[:14]) / 14
    for gain, loss in zip(gains[14:], losses[14:]):
        average_gain = (average_gain * 13 + gain) / 14
        average_loss = (average_loss * 13 + loss) / 14

    if average_gain == 0 and average_loss == 0:
        rsi = 50.0
    elif average_loss == 0:
        rsi = 100.0
    elif average_gain == 0:
        rsi = 0.0
    else:
        relative_strength = average_gain / average_loss
        rsi = 100 - 100 / (1 + relative_strength)

    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd_values = [
        fast_value - slow_value
        for fast_value, slow_value in zip(fast, slow)
        if fast_value is not None and slow_value is not None
    ]
    signal_values = _ema(macd_values, 9)
    macd_signal = signal_values[-1]
    if macd_signal is None:
        raise ValueError("Not enough candles to calculate MACD signal")
    ema50 = _ema(closes, 50)[-1]
    if ema50 is None:
        raise ValueError("Not enough candles to calculate EMA(50)")

    return Indicators(
        rsi=rsi,
        macd=macd_values[-1],
        macd_signal=macd_signal,
        close=closes[-1],
        ema50=ema50,
    )


def matching_setups(indicators: Indicators) -> dict[str, bool]:
    return {
        "overbought_bullish": indicators.rsi > 70
        and indicators.macd > indicators.macd_signal
        and indicators.close > indicators.ema50,
        "oversold_bearish": indicators.rsi < 30
        and indicators.macd < indicators.macd_signal
        and indicators.close < indicators.ema50,
    }
