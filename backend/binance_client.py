import httpx
import time
from typing import List, Dict

BINANCE_BASE = "https://api.binance.com"


async def get_historical_candles(limit: int = 200) -> List[Dict]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "5m", "limit": limit}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        raw = resp.json()

    candles = []
    for k in raw:
        candles.append({
            "time": int(k[0]) // 1000,  # seconds for TradingView
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


async def get_current_price() -> float:
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(url, params={"symbol": "BTCUSDT"})
        resp.raise_for_status()
        return float(resp.json()["price"])
