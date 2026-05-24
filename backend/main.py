from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio

from binance_client import get_historical_candles, get_current_price
from polymarket_client import get_current_market
from indicators import calculate_signals
from predictor import get_advice

app = FastAPI(title="BTC Polymarket Advisor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/data")
async def get_data():
    try:
        candles_task = get_historical_candles(200)
        market_task = get_current_market()
        price_task = get_current_price()

        candles, market, current_price = await asyncio.gather(
            candles_task, market_task, price_task,
            return_exceptions=True
        )

        if isinstance(candles, Exception):
            return JSONResponse(status_code=502, content={"error": f"Binance error: {candles}"})

        if isinstance(market, Exception):
            market = None

        if isinstance(current_price, Exception):
            current_price = candles[-1]["close"] if candles else 0

        signals = calculate_signals(candles)
        advice = get_advice(signals, market)

        return {
            "candles": candles,
            "current_price": current_price,
            "market": market,
            "signals": signals,
            "advice": advice,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/candles")
async def get_candles():
    candles = await get_historical_candles(200)
    return {"candles": candles}


@app.get("/health")
async def health():
    return {"status": "ok"}
