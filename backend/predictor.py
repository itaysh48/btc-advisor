from typing import Dict, Optional, List


def get_advice(signals: Dict, market: Optional[Dict]) -> Dict:
    score = 0.0
    breakdown = {}

    # 1. Momentum (30%) — average direction of last 3 candles
    mom = signals.get("momentum", 0.0)
    mom_score = mom  # already -1 to +1
    score += mom_score * 0.30
    breakdown["momentum"] = {
        "value": round(mom * 100),
        "signal": "UP" if mom > 0.2 else ("DOWN" if mom < -0.2 else "NEUTRAL"),
        "weight": 30,
    }

    # 2. RSI(9) (25%) — linear scale: <30 = +1, >70 = -1, middle = linear
    rsi = signals.get("rsi", 50.0)
    if rsi <= 30:
        rsi_score = 1.0
    elif rsi >= 70:
        rsi_score = -1.0
    else:
        rsi_score = -(rsi - 50) / 20.0  # linear: 50→0, 30→+1, 70→-1
    score += rsi_score * 0.25
    breakdown["rsi"] = {
        "value": rsi,
        "signal": "UP" if rsi_score > 0.3 else ("DOWN" if rsi_score < -0.3 else "NEUTRAL"),
        "weight": 25,
    }

    # 3. EMA(9) vs EMA(21) (20%) — trend direction
    ema9 = signals.get("ema9")
    ema21 = signals.get("ema21")
    if ema9 is not None and ema21 is not None and ema21 > 0:
        ema_diff_pct = (ema9 - ema21) / ema21 * 100
        # cap at ±0.1%
        ema_score = max(-1.0, min(1.0, ema_diff_pct / 0.1))
        ema_signal = "UP" if ema_score > 0.2 else ("DOWN" if ema_score < -0.2 else "NEUTRAL")
    else:
        ema_score = 0.0
        ema_signal = "NEUTRAL"
    score += ema_score * 0.20
    breakdown["ema"] = {
        "value": f"{ema9:.0f} / {ema21:.0f}" if ema9 and ema21 else "N/A",
        "signal": ema_signal,
        "weight": 20,
    }

    # 4. Volume (15%) — directional: high volume in direction of momentum
    vol_ratio = signals.get("volume_ratio", 1.0)
    # volume above 1.5x amplifies momentum signal
    if vol_ratio > 1.5:
        vol_score = mom * min(vol_ratio / 1.5, 2.0) / 2.0  # amplify momentum
    else:
        vol_score = 0.0
    score += vol_score * 0.15
    breakdown["volume"] = {
        "value": f"{vol_ratio:.1f}x",
        "signal": "HIGH" if vol_ratio > 1.5 else ("LOW" if vol_ratio < 0.7 else "NORMAL"),
        "weight": 15,
    }

    # 5. Polymarket odds (10%) — smart money signal
    if market:
        up_prob = market.get("up_probability", 50.0)
        pm_score = (up_prob - 50) / 20.0  # 70%→+1, 30%→-1
        pm_score = max(-1.0, min(1.0, pm_score))
        pm_signal = "UP" if up_prob > 60 else ("DOWN" if up_prob < 40 else "NEUTRAL")
    else:
        pm_score = 0.0
        pm_signal = "NEUTRAL"
        up_prob = 50.0
    score += pm_score * 0.10
    breakdown["polymarket"] = {
        "value": f"{up_prob:.0f}%",
        "signal": pm_signal,
        "weight": 10,
    }

    # Final recommendation
    if score > 0.15:
        recommendation = "UP"
    elif score < -0.15:
        recommendation = "DOWN"
    else:
        recommendation = "NEUTRAL"

    # Confidence: how far from neutral, scaled to 50-95%
    confidence = min(95, 50 + abs(score) * 150)

    return {
        "recommendation": recommendation,
        "confidence": round(confidence),
        "score": round(score, 3),
        "signals": breakdown,
        "reasoning": _build_reasoning(recommendation, breakdown, signals),
    }


def _build_reasoning(rec: str, breakdown: Dict, signals: Dict) -> str:
    parts = []
    mom_pct = breakdown["momentum"]["value"]
    parts.append(f"מומנטום {'+' if mom_pct >= 0 else ''}{mom_pct}%")
    parts.append(f"RSI {signals.get('rsi', 50):.0f}")
    vol = breakdown["volume"]["value"]
    parts.append(f"נפח {vol}")
    pm = breakdown["polymarket"]["value"]
    parts.append(f"פולימרקט {pm} UP")
    return " | ".join(parts)
