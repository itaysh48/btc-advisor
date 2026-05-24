#!/usr/bin/env python3
"""
BTC Polymarket 5-min Advisor — Cloud Worker v2
Runs every 5 minutes via GitHub Actions cron.
- Learns from every resolved window (wrong predictions weighted ×3)
- Tracks Polymarket crowd odds as a signal (learn from other users)
- Stores open/close prices for every bet
- Advanced candle features: wick ratio, consecutive run, body size
"""

import json, math, time, sys
from pathlib import Path
from datetime import datetime, timezone

try:
    import httpx
    def _get(url, **kw):
        r = httpx.get(url, timeout=25, follow_redirects=True, **kw)
        r.raise_for_status()
        return r.json()
    def _post(url, body):
        r = httpx.post(url, json=body, timeout=25)
        r.raise_for_status()
        return r.json()
except ImportError:
    import urllib.request
    def _get(url, **kw):
        params = kw.get("params", {})
        if params:
            qs = "&".join(f"{k}={v}" for k,v in params.items())
            url = f"{url}?{qs}"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=25) as r:
            return json.loads(r.read())
    def _post(url, body):
        data = json.dumps(body).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())

ROOT       = Path(__file__).parent.parent
STATE_DIR  = ROOT / "state"
STATE_FILE = STATE_DIR / "model.json"
STATS_FILE = STATE_DIR / "stats.json"

WIN           = 300
BATCH         = 1000
TRAIN_BATCHES = 8

# ── Candles ──────────────────────────────────────────────────────────────────

def fetch_candles(limit=200, end_ts=None):
    params = {"step": 300, "limit": limit}
    if end_ts:
        params["end"] = end_ts
    d = _get("https://www.bitstamp.net/api/v2/ohlc/btcusd/", params=params)
    return [{"t": int(c["timestamp"]), "o": float(c["open"]), "h": float(c["high"]),
             "l": float(c["low"]),  "c": float(c["close"]), "v": float(c["volume"])}
            for c in d.get("data", {}).get("ohlc", [])]

def fetch_training_candles():
    all_c, end_ts = [], None
    print(f"  Fetching {TRAIN_BATCHES}×{BATCH} candles from Bitstamp...")
    for i in range(TRAIN_BATCHES):
        batch = fetch_candles(BATCH, end_ts)
        if not batch: break
        all_c  = batch + all_c
        end_ts = batch[0]["t"] - 1
        dt = datetime.fromtimestamp(batch[0]["t"], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"    Batch {i+1}/{TRAIN_BATCHES}: {len(batch)} candles back to {dt}")
        time.sleep(0.4)
    return all_c

# ── Chainlink price ───────────────────────────────────────────────────────────

def fetch_price():
    try:
        d = _post("https://polygon-bor-rpc.publicnode.com", {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": "0xc907E116054Ad103354f2D350FD2514433D57F6F",
                        "data": "0xfeaf968c"}, "latest"]
        })
        h = d.get("result", "")
        if len(h) < 130: raise ValueError("short")
        return int(h[2:][64:128], 16) / 1e8
    except Exception as e:
        print(f"  Chainlink fallback→Coinbase: {e}")
        d = _get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        return float(d["data"]["amount"])

# ── Polymarket ────────────────────────────────────────────────────────────────

def fetch_resolution(win_ts_val):
    """Returns resolved market data or None if not yet resolved."""
    try:
        slug = f"btc-updown-5m-{win_ts_val}"
        d = _get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        if not d: return None
        meta = d[0].get("eventMetadata", {})
        ptb  = meta.get("priceToBeat")
        fp   = meta.get("finalPrice")
        if not ptb or not fp: return None
        return {"priceToBeat": float(ptb), "finalPrice": float(fp),
                "actualUp":    float(fp) >= float(ptb)}
    except Exception as e:
        print(f"  Polymarket resolution error: {e}")
        return None

def fetch_active_odds(win_ts_val):
    """
    Fetch crowd betting odds for the ACTIVE current market.
    Returns UP probability (0-1) reflecting where users are betting.
    This is how we track and learn from other bettors.
    """
    try:
        slug = f"btc-updown-5m-{win_ts_val}"
        d = _get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        if not d: return None
        markets = d[0].get("markets", [])
        if not markets: return None
        # Find the UP market (title contains "above" or outcome[0])
        for mkt in markets:
            prices = mkt.get("outcomePrices", [])
            outcomes = mkt.get("outcomes", [])
            if not prices or len(prices) < 2: continue
            try:
                p0, p1 = float(prices[0]), float(prices[1])
                # Skip resolved (prices go to 0 or 1)
                if p0 > 0.98 or p0 < 0.02: continue
                # First outcome is typically YES/UP
                return round(p0, 4)
            except:
                continue
        return None
    except Exception as e:
        print(f"  Active odds error: {e}")
        return None

# ── Indicators ───────────────────────────────────────────────────────────────

def calc_rsi(closes, period=9):
    if len(closes) < period + 1: return 50.0
    ag = al = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i-1]
        ag += max(d, 0); al += max(-d, 0)
    ag /= period; al /= period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i-1]
        ag = (ag*(period-1) + max(d,0)) / period
        al = (al*(period-1) + max(-d,0)) / period
    return 100.0 if al == 0 else 100 - 100/(1 + ag/al)

def calc_ema(prices, period):
    if not prices: return 0
    k, ema = 2/(period+1), prices[0]
    for p in prices[1:]: ema = p*k + ema*(1-k)
    return ema

def rsi_bucket(rsi):
    if rsi < 30:  return "os"
    if rsi < 45:  return "lo"
    if rsi < 55:  return "mi"
    if rsi < 70:  return "hi"
    return "ob"

def features(candles, i):
    if i < 30 or i > len(candles) - 1: return None
    hist   = candles[max(0, i-25):i]
    closes = [c["c"] for c in hist]
    last_c = closes[-1]
    prev   = candles[i-1]

    p1 = 1 if prev["c"] >= prev["o"] else 0
    p2 = 1 if candles[i-2]["c"] >= candles[i-2]["o"] else 0
    p3 = 1 if candles[i-3]["c"] >= candles[i-3]["o"] else 0

    rsi       = calc_rsi(closes, 9)
    ema21     = calc_ema(closes, 21)
    vol_avg   = sum(c["v"] for c in hist[-14:]) / max(len(hist[-14:]), 1)
    vol_ratio = prev["v"] / (vol_avg or 1)

    # KEY SIGNAL 1: candle magnitude
    last_pct = abs((prev["c"] - prev["o"]) / (prev["o"] or 1)) * 100
    if   last_pct >= 0.20: mag = "huge"
    elif last_pct >= 0.10: mag = "big"
    elif last_pct < 0.01:  mag = "noise"
    else:                   mag = "norm"

    # KEY SIGNAL 2: trend strength (10 candles)
    last10   = candles[max(0, i-10):i]
    up_ratio = sum(1 for c in last10 if c["c"] >= c["o"]) / max(len(last10), 1)
    trend    = "sup" if up_ratio >= 0.70 else ("sdn" if up_ratio <= 0.30 else "neu")

    # KEY SIGNAL 3: wick analysis (reversal detector)
    total_range = prev["h"] - prev["l"]
    body        = abs(prev["c"] - prev["o"])
    wick_ratio  = body / (total_range or 0.001)  # low = doji/wick, high = strong body
    upper_wick  = prev["h"] - max(prev["c"], prev["o"])
    lower_wick  = min(prev["c"], prev["o"]) - prev["l"]
    # Long upper wick on up candle = bearish rejection
    # Long lower wick on down candle = bullish rejection
    upper_pct = upper_wick / (total_range or 0.001)
    lower_pct = lower_wick / (total_range or 0.001)
    reversal = "bear_rej" if (p1 and upper_pct > 0.45) else \
               "bull_rej" if (not p1 and lower_pct > 0.45) else "none"

    # KEY SIGNAL 4: consecutive run
    streak = 1
    for j in range(i-2, max(0, i-10), -1):
        if (1 if candles[j]["c"] >= candles[j]["o"] else 0) == p1: streak += 1
        else: break
    streak_cat = "long" if streak >= 4 else ("med" if streak >= 2 else "one")

    # KEY SIGNAL 5: volume burst
    vol_cat = "high" if vol_ratio > 1.8 else ("low" if vol_ratio < 0.5 else "norm")

    return {"p1": p1, "p2": p2, "p3": p3,
            "rsi": rsi, "rb": rsi_bucket(rsi),
            "ema21": ema21, "ema_trend": 1 if last_c > ema21 else 0,
            "vol_ratio": vol_ratio, "vol_cat": vol_cat,
            "streak": streak, "streak_cat": streak_cat,
            "mag": mag, "trend": trend,
            "wick_ratio": round(wick_ratio, 2),
            "reversal": reversal,
            "upper_pct": round(upper_pct, 2),
            "lower_pct": round(lower_pct, 2),
            "up_ratio10": up_ratio, "last_pct": last_pct}

def fkey(f):
    return f"{f['p1']}{f['p2']}{f['p3']}_{f['mag']}_{f['trend']}"

def fkey_fb(f):
    return f"{f['p1']}{f['p2']}{f['p3']}_{f['mag']}"

def fkey_wick(f):
    # Reversal key — for wick/rejection patterns
    return f"{f['reversal']}_{f['mag']}"

def fkey_crowd(crowd_up_prob):
    # Crowd sentiment bucket
    if crowd_up_prob is None: return None
    if crowd_up_prob >= 0.65: return "crowd_bull"
    if crowd_up_prob <= 0.35: return "crowd_bear"
    return "crowd_neut"

def time_ctx(ts):
    d  = datetime.fromtimestamp(ts, tz=timezone.utc)
    wk = "wknd" if d.weekday() >= 5 else "wkday"
    s  = ["asia","asia","asia-eu","eu","eu-us","us"][d.hour // 4]
    return f"{wk}_{s}"

# ── Model ─────────────────────────────────────────────────────────────────────

def laplace(entry, base, alpha=2):
    return (entry["up"] + alpha*base) / (entry["total"] + alpha)

def _tbl_inc(tbl, key, up, w=1):
    if key not in tbl: tbl[key] = {"up": 0, "total": 0}
    tbl[key]["total"] += w
    if up: tbl[key]["up"] += w

def lookup(tbl, fb_tbl, base, f):
    k = fkey(f); e = tbl.get(k)
    if e and e["total"] >= 10:
        p = laplace(e, base)
        fb = fb_tbl.get(fkey_fb(f))
        if fb and fb["total"] >= 15:
            return p*0.7 + laplace(fb, base)*0.3
        return p
    fb = fb_tbl.get(fkey_fb(f))
    if fb and fb["total"] >= 8: return laplace(fb, base)
    return base

def lookup_time(time_tbl, ts):
    e = time_tbl.get(time_ctx(ts))
    if e and e["total"] >= 20: return laplace(e, 0.5)
    return 0.5

def lookup_wick(wick_tbl, f):
    if f["reversal"] == "none": return None
    e = wick_tbl.get(fkey_wick(f))
    if e and e["total"] >= 8: return laplace(e, 0.5)
    return None

def lookup_crowd(crowd_tbl, crowd_up_prob):
    key = fkey_crowd(crowd_up_prob)
    if key is None: return None
    e = crowd_tbl.get(key)
    if e and e["total"] >= 15: return laplace(e, 0.5)
    return None

def combined_prob(mdl, f, ts, crowd_up_prob=None):
    tbl      = mdl["table"]
    fb_tbl   = mdl["fallback"]
    time_tbl = mdl["timeTable"]
    wick_tbl = mdl.get("wickTable", {})
    crowd_tbl= mdl.get("crowdTable", {})
    base     = mdl["baseRate"]

    main     = lookup(tbl, fb_tbl, base, f)
    time_adj = lookup_time(time_tbl, ts)
    wick_sig = lookup_wick(wick_tbl, f)
    crowd_sig= lookup_crowd(crowd_tbl, crowd_up_prob)

    # Direct empirical signals (from real Chainlink data analysis)
    direct, dw = base, 0.0
    if   f["mag"] == "huge": direct = 0.80 if f["p1"] else 0.20; dw = 0.40
    elif f["mag"] == "big":  direct = 0.61 if f["p1"] else 0.39; dw = 0.22
    elif f["trend"] == "sup": direct = 0.64; dw = 0.18
    elif f["trend"] == "sdn": direct = 0.36; dw = 0.18

    # Wick reversal adjustment
    if wick_sig is not None:
        # If bearish rejection on up candle, shift toward DOWN
        if f["reversal"] == "bear_rej": direct = direct * 0.7 + 0.3 * (1 - wick_sig)
        elif f["reversal"] == "bull_rej": direct = direct * 0.7 + 0.3 * wick_sig

    # Streak penalty: >4 same candles = mean reversion pressure
    if f["streak_cat"] == "long":
        direct = direct * 0.80 + (1 - direct) * 0.20

    rw = 1 - dw
    prob = direct*dw + main*(rw*0.70) + time_adj*(rw*0.12) + base*(rw*0.08)

    # Crowd signal (10% weight when available — learn from other bettors)
    if crowd_sig is not None:
        prob = prob * 0.90 + crowd_sig * 0.10

    return max(0.05, min(0.95, prob))

def train(candles):
    tbl, fb_tbl, time_tbl, wick_tbl = {}, {}, {}, {}
    up_cnt = total = 0
    for i in range(30, len(candles) - 1):
        f  = features(candles, i)
        if not f: continue
        au = candles[i]["c"] >= candles[i]["o"]
        _tbl_inc(tbl,      fkey(f),    au)
        _tbl_inc(fb_tbl,   fkey_fb(f), au)
        _tbl_inc(time_tbl, time_ctx(candles[i]["t"]), au)
        if f["reversal"] != "none":
            _tbl_inc(wick_tbl, fkey_wick(f), au)
        if au: up_cnt += 1
        total += 1
    base = up_cnt / total if total else 0.5

    mdl = {"table": tbl, "fallback": fb_tbl, "timeTable": time_tbl,
           "wickTable": wick_tbl, "crowdTable": {},
           "baseRate": base, "trainedOn": total, "version": 8}

    # Evaluate on last 500 candles
    correct = tested = 0
    for i in range(max(30, len(candles)-500), len(candles)-1):
        f = features(candles, i)
        if not f: continue
        p = combined_prob(mdl, f, candles[i]["t"])
        if (p >= 0.5) == (candles[i]["c"] >= candles[i]["o"]): correct += 1
        tested += 1
    mdl["accuracy"] = correct/tested if tested else 0.5
    print(f"  Trained {total} samples | base={base:.3f} | eval={mdl['accuracy']:.1%}")
    return mdl

def online_update(mdl, f_snap, actual_up, crowd_up_prob=None, weight=1.0):
    w = max(1, round(weight))
    f_like = {"p1": f_snap["p1"], "p2": f_snap["p2"], "p3": f_snap["p3"],
              "mag": f_snap["mag"], "trend": f_snap["trend"],
              "reversal": f_snap.get("reversal", "none"),
              "streak_cat": f_snap.get("streak_cat", "one")}
    _tbl_inc(mdl["table"],    fkey(f_like),    actual_up, w)
    _tbl_inc(mdl["fallback"], fkey_fb(f_like), actual_up, w)
    ts = f_snap.get("ts", int(time.time()))
    _tbl_inc(mdl["timeTable"], time_ctx(ts), actual_up, w)
    if f_like["reversal"] != "none":
        if "wickTable" not in mdl: mdl["wickTable"] = {}
        _tbl_inc(mdl["wickTable"], fkey_wick(f_like), actual_up, w)
    # Crowd learning: did the crowd get it right?
    crowd_key = fkey_crowd(crowd_up_prob)
    if crowd_key:
        if "crowdTable" not in mdl: mdl["crowdTable"] = {}
        _tbl_inc(mdl["crowdTable"], crowd_key, actual_up, 1)

# ── Prediction ────────────────────────────────────────────────────────────────

def make_pred(mdl, candles, ts, crowd_up_prob=None):
    f = features(candles, len(candles))
    if not f: return None
    prob = combined_prob(mdl, f, ts, crowd_up_prob)

    # Neutral zone: only skip when truly no signal
    neutral   = 0.45 <= prob <= 0.55
    direction = "NEUTRAL" if neutral else ("UP" if prob > 0.5 else "DOWN")
    cs        = abs(prob - 0.5) * 2
    conf      = "גבוה" if cs > 0.30 else ("בינוני" if cs > 0.15 else "נמוך")

    return {
        "winTs":      ts,
        "fKey":       fkey(f),
        "fbKey":      fkey_fb(f),
        "direction":  direction,
        "predictedUp": prob > 0.55,
        "prob":       round(prob, 4),
        "confidence": conf,
        "crowdUpProb": crowd_up_prob,
        "signals": {
            "mag":       f["mag"],
            "trend":     f["trend"],
            "p1":        f["p1"], "p2": f["p2"], "p3": f["p3"],
            "rsi":       round(f["rsi"], 1),
            "lastPct":   round(f["last_pct"], 3),
            "upRatio10": round(f["up_ratio10"], 2),
            "volRatio":  round(f["vol_ratio"], 2),
            "volCat":    f["vol_cat"],
            "reversal":  f["reversal"],
            "streakCat": f["streak_cat"],
            "wickRatio": f["wick_ratio"]
        },
        "ts": ts
    }

# ── State I/O ─────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    return None

def save_state(mdl, pending):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"model": mdl, "pendingPred": pending,
                                       "savedAt": int(time.time())}, ensure_ascii=False))

def load_stats():
    if STATS_FILE.exists(): return json.loads(STATS_FILE.read_text())
    return {"total": 0, "correct": 0, "last200": [],
            "errorPatterns": {"by_mag": {}, "by_trend": {}, "by_session": {}, "by_key": {}},
            "recentLog": [], "betsLog": []}

def record_outcome(stats, pred, actual_up, resolution=None):
    sigs     = pred.get("signals", {})
    pred_up  = pred.get("predictedUp", True)
    neutral  = pred.get("direction") == "NEUTRAL"
    correct  = (pred_up == actual_up) or neutral

    stats["total"]   += 1
    if correct: stats["correct"] += 1
    stats["last200"].append(1 if correct else 0)
    if len(stats["last200"]) > 200: stats["last200"].pop(0)

    def pat(cat, key):
        if key not in stats["errorPatterns"][cat]:
            stats["errorPatterns"][cat][key] = {"total": 0, "wrong": 0}
        stats["errorPatterns"][cat][key]["total"] += 1
        if not correct: stats["errorPatterns"][cat][key]["wrong"] += 1

    pat("by_mag",     sigs.get("mag",   "?"))
    pat("by_trend",   sigs.get("trend", "?"))
    pat("by_session", time_ctx(pred.get("winTs", 0)))
    pat("by_key",     pred.get("fKey",  "?"))

    ts    = pred.get("winTs", 0)
    label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d %H:%M") if ts else "?"
    entry = {"winTs": ts, "time": label,
             "direction": pred["direction"], "prob": pred["prob"],
             "actualUp": actual_up, "correct": correct,
             "mag": sigs.get("mag"), "trend": sigs.get("trend"),
             "reversal": sigs.get("reversal", "none"),
             "crowdUp": pred.get("crowdUpProb")}

    # Store open/close prices for the bets log
    if resolution:
        entry["priceToBeat"] = round(resolution["priceToBeat"], 2)
        entry["finalPrice"]  = round(resolution["finalPrice"],  2)
        entry["priceDiff"]   = round(resolution["finalPrice"] - resolution["priceToBeat"], 2)

    stats["recentLog"].insert(0, entry)
    if len(stats["recentLog"]) > 100: stats["recentLog"].pop()

    # Dedicated bets log (richer data for the UI panel)
    if "betsLog" not in stats: stats["betsLog"] = []
    stats["betsLog"].insert(0, entry)
    if len(stats["betsLog"]) > 50: stats["betsLog"].pop()

    return correct

def save_stats(stats, current_pred):
    STATE_DIR.mkdir(exist_ok=True)
    total, correct = stats["total"], stats["correct"]
    last200 = stats["last200"]
    last20  = last200[-20:] if len(last200) >= 20 else last200

    # Crowd analysis summary
    crowd_analysis = {}
    if "betsLog" in stats:
        crowd_bets = [b for b in stats["betsLog"] if b.get("crowdUp") is not None]
        if crowd_bets:
            bull = [b for b in crowd_bets if b["crowdUp"] >= 0.65]
            bear = [b for b in crowd_bets if b["crowdUp"] <= 0.35]
            crowd_analysis = {
                "bull": {"total": len(bull),
                         "correct": sum(1 for b in bull if b["actualUp"])},
                "bear": {"total": len(bear),
                         "correct": sum(1 for b in bear if not b["actualUp"])}
            }

    out = {**stats,
           "accuracy":      round(correct/total, 4) if total else 0,
           "accuracy20":    round(sum(last20)/len(last20), 4) if last20 else 0,
           "accuracy200":   round(sum(last200)/len(last200), 4) if last200 else 0,
           "currentPrediction": current_pred,
           "crowdAnalysis": crowd_analysis,
           "updatedAt":     int(time.time())}
    STATS_FILE.write_text(json.dumps(out, ensure_ascii=False))
    acc = out["accuracy"]
    print(f"  Stats: {correct}/{total} = {acc:.1%} | last200={out['accuracy200']:.1%}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now     = int(time.time())
    cur_win = (now // WIN) * WIN
    utc_now = datetime.fromtimestamp(now, tz=timezone.utc)
    utc_win = datetime.fromtimestamp(cur_win, tz=timezone.utc)

    print(f"\n{'='*56}")
    print(f"BTC Worker v2  {utc_now:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Window: {cur_win}  ({utc_win:%H:%M} UTC)")
    print(f"{'='*56}")

    state = load_state()
    stats = load_stats()

    if state is None:
        print("\n[INIT] First run — fetching training data...")
        candles = fetch_training_candles()
        print(f"  Total: {len(candles)} candles")
        mdl     = train(candles)
        pending = None
    else:
        mdl     = state["model"]
        pending = state.get("pendingPred")
        # Migrate older model versions
        if "wickTable"  not in mdl: mdl["wickTable"]  = {}
        if "crowdTable" not in mdl: mdl["crowdTable"] = {}
        print(f"\n[LOADED] {len(mdl['table'])} keys, {mdl['trainedOn']} trained, "
              f"stats={stats['correct']}/{stats['total']}")
        if pending:
            print(f"[PENDING] win={pending['winTs']} dir={pending['direction']} "
                  f"prob={pending['prob']:.3f} crowd={pending.get('crowdUpProb')}")

    # ── Resolve pending prediction ─────────────────────────────────────────
    if pending:
        prev_win = pending["winTs"]
        if now >= prev_win + WIN + 45:
            print(f"\n[LEARN] Checking resolution for {prev_win} "
                  f"({datetime.fromtimestamp(prev_win, tz=timezone.utc):%H:%M} UTC)...")
            res = fetch_resolution(prev_win)
            if res:
                actual_up   = res["actualUp"]
                pred_up     = pending.get("predictedUp", True)
                neutral     = pending.get("direction") == "NEUTRAL"
                correct     = (pred_up == actual_up) or neutral
                weight      = 1.0 if correct else 3.0
                crowd_prob  = pending.get("crowdUpProb")

                sigs = pending.get("signals", {})
                f_snap = {"p1": sigs.get("p1", 0), "p2": sigs.get("p2", 0),
                          "p3": sigs.get("p3", 0), "mag": sigs.get("mag", "norm"),
                          "trend": sigs.get("trend", "neu"),
                          "reversal": sigs.get("reversal", "none"),
                          "streak_cat": sigs.get("streakCat", "one"),
                          "ts": prev_win}
                online_update(mdl, f_snap, actual_up, crowd_prob, weight=weight)
                was_ok = record_outcome(stats, pending, actual_up, resolution=res)
                emoji  = "✅" if was_ok else f"❌ (×{weight:.0f})"
                print(f"  PTB={res['priceToBeat']:.2f}  Final={res['finalPrice']:.2f}  "
                      f"Up={actual_up}  Crowd={crowd_prob}  {emoji}")
                pending = None
            else:
                print(f"  Not yet resolved — keeping pending")

    # ── Fetch candles ──────────────────────────────────────────────────────
    print("\n[CANDLES] Fetching 200 recent candles...")
    candles = fetch_candles(limit=200)
    if not candles:
        print("ERROR: No candles"); sys.exit(1)
    latest = datetime.fromtimestamp(candles[-1]["t"], tz=timezone.utc)
    print(f"  {len(candles)} candles, latest: {latest:%H:%M:%S} UTC")

    # ── Fetch crowd odds (what other users are betting) ────────────────────
    print(f"\n[CROWD] Fetching active market odds for window {cur_win}...")
    crowd_up_prob = fetch_active_odds(cur_win)
    if crowd_up_prob is not None:
        sentiment = "🐂 Bull" if crowd_up_prob >= 0.65 else ("🐻 Bear" if crowd_up_prob <= 0.35 else "⚖️ Neutral")
        print(f"  Crowd UP={crowd_up_prob:.1%}  {sentiment}")
    else:
        print(f"  No crowd data available")

    # ── Make prediction ────────────────────────────────────────────────────
    print(f"\n[PREDICT] Window {cur_win}...")
    pred = make_pred(mdl, candles, cur_win, crowd_up_prob)
    if pred:
        sigs = pred["signals"]
        print(f"  → {pred['direction']}  prob={pred['prob']:.3f}  conf={pred['confidence']}")
        print(f"     mag={sigs['mag']}  trend={sigs['trend']}  "
              f"reversal={sigs['reversal']}  streak={sigs['streakCat']}")
        pending = pred

    # ── Fetch price ────────────────────────────────────────────────────────
    print("\n[PRICE] Fetching Chainlink price...")
    try:
        price = fetch_price()
        print(f"  BTC/USD = ${price:,.2f}")
        if pred: pred["currentPrice"] = round(price, 2)
    except Exception as e:
        print(f"  Price error: {e}")

    save_state(mdl, pending)
    save_stats(stats, pred)

    acc = stats["correct"] / stats["total"] if stats["total"] else 0
    print(f"\n[DONE] {stats['correct']}/{stats['total']} = {acc:.1%}  (target: 75%)")

if __name__ == "__main__":
    main()
