import httpx
import time
from typing import Optional, Dict

GAMMA_BASE = "https://gamma-api.polymarket.com"
WINDOW = 300  # 5 minutes in seconds


def current_window_ts() -> int:
    return (int(time.time()) // WINDOW) * WINDOW


def next_window_ts() -> int:
    return current_window_ts() + WINDOW


async def get_current_market() -> Optional[Dict]:
    ts = current_window_ts()
    slug = f"btc-updown-5m-{ts}"
    url = f"{GAMMA_BASE}/markets"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={"slug": slug})
        if resp.status_code != 200:
            return None
        data = resp.json()

    markets = data if isinstance(data, list) else data.get("markets", [])
    if not markets:
        # Try the next window in case current hasn't opened yet
        ts = next_window_ts()
        slug = f"btc-updown-5m-{ts}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params={"slug": slug})
            if resp.status_code != 200:
                return None
            data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])

    if not markets:
        return None

    m = markets[0]

    # outcome prices: index 0 = UP, index 1 = DOWN
    outcome_prices = []
    raw_prices = m.get("outcomePrices", "[]")
    if isinstance(raw_prices, str):
        import json
        try:
            outcome_prices = json.loads(raw_prices)
        except Exception:
            outcome_prices = []
    elif isinstance(raw_prices, list):
        outcome_prices = raw_prices

    up_prob = float(outcome_prices[0]) * 100 if len(outcome_prices) > 0 else 50.0
    down_prob = float(outcome_prices[1]) * 100 if len(outcome_prices) > 1 else 50.0

    return {
        "market_id": m.get("id"),
        "slug": m.get("slug"),
        "question": m.get("question", "BTC Up or Down?"),
        "up_probability": round(up_prob, 1),
        "down_probability": round(down_prob, 1),
        "closes_at": next_window_ts(),
        "seconds_remaining": next_window_ts() - int(time.time()),
        "url": f"https://polymarket.com/event/{m.get('slug', slug)}",
    }
