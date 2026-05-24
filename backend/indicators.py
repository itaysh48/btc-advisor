import numpy as np
from typing import List, Dict


def calculate_rsi(closes: List[float], period: int = 9) -> List[float]:
    closes = np.array(closes, dtype=float)
    if len(closes) < period + 1:
        return [50.0] * len(closes)

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    rsi_values = [np.nan] * period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100 - 100 / (1 + rs))

    # pad beginning
    result = [50.0] * (len(closes) - len(rsi_values)) + rsi_values
    return result


def calculate_ema(closes: List[float], period: int) -> List[float]:
    closes = np.array(closes, dtype=float)
    k = 2.0 / (period + 1)
    ema = [closes[0]]
    for price in closes[1:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def calculate_signals(candles: List[Dict]) -> Dict:
    if len(candles) < 25:
        return {
            "rsi": 50.0,
            "ema9": None,
            "ema21": None,
            "momentum": 0.0,
            "volume_ratio": 1.0,
            "last_close": candles[-1]["close"] if candles else 0,
        }

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    rsi_series = calculate_rsi(closes, 9)
    ema9_series = calculate_ema(closes, 9)
    ema21_series = calculate_ema(closes, 21)

    rsi = rsi_series[-1]
    ema9 = ema9_series[-1]
    ema21 = ema21_series[-1]

    # Momentum: direction of last 3 completed candles
    last3_dirs = []
    for i in range(-4, -1):
        c = candles[i]
        last3_dirs.append(1 if c["close"] >= c["open"] else -1)
    momentum = sum(last3_dirs) / 3.0  # -1 to +1

    # Volume: current vs 20-period average
    avg_vol = np.mean(volumes[-21:-1]) if len(volumes) > 21 else np.mean(volumes)
    current_vol = volumes[-1]
    volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

    return {
        "rsi": round(rsi, 1),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "momentum": round(momentum, 3),
        "volume_ratio": round(volume_ratio, 2),
        "last_close": closes[-1],
    }
