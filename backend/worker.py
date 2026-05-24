#!/usr/bin/env python3
"""
BTC Polymarket 5-min Advisor — Cloud Worker
Runs every 5 minutes via GitHub Actions cron.
Learns from every resolved window, stores unlimited stats.
"""

import json, math, time, sys, os
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
    import urllib.request, urllib.error
    def _get(url, **kw):
        params = kw.get("params", {})
        if params:
            qs = "&".join(f"{k}={v}" for k,v in params.items())
            url = f"{url}?{qs}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    def _post(url, body):
        import urllib.parse
        data = json.dumps(body).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
STATE_DIR  = ROOT / "state"
STATE_FILE = STATE_DIR / "model.json"
STATS_FILE = STATE_DIR / "stats.json"

WIN           = 300
BATCH         = 1000
TRAIN_BATCHES = 8   # ~27 days

# ── Candles (Bitstamp) ───────────────────────────────────────────────────────

def fetch_candles(limit=200, end_ts=None):
    params = {"step": 300, "limit": limit}
    if end_ts:
        params["end"] = end_ts
    d = _get("https://www.bitstamp.net/api/v2/ohlc/btcusd/", params=params)
    return [{"t": int(c["timestamp"]), "o": float(c["open"]), "h": float(c["high"]),
             "l": float(c["low"]),  "c": float(c["close"]), "v": float(c["volume"])}
            for c in d.get("data", {}).get("ohlc", [])]

def fetch_training_candles():
    all_c   = []
    end_ts  = None
    print(f"  Fetching {TRAIN_BATCHES} × {BATCH} candles from Bitstamp...")
    for i in range(TRAIN_BATCHES):
        batch = fetch_candles(BATCH, end_ts)
        if not batch:
            break
        all_c  = batch + all_c
        end_ts = batch[0]["t"] - 1
        dt = datetime.fromtimestamp(batch[0]["t"], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"    Batch {i+1}/{TRAIN_BATCHES}: {len(batch)} candles back to {dt}")
        time.sleep(0.4)
    return all_c

# ── Chainlink price (Polygon RPC) ────────────────────────────────────────────

def fetch_price():
    try:
        d = _post("https://polygon-bor-rpc.publicnode.com", {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": "0xc907E116054Ad103354f2D350FD2514433D57F6F",
                        "data": "0xfeaf968c"}, "latest"]
        })
        h = d.get("result", "")
        if len(h) < 130:
            raise ValueError("short response")
        return int(h[2:][64:128], 16) / 1e8
    except Exception as e:
        print(f"  Chainlink fallback → Coinbase: {e}")
        d = _get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        return float(d["data"]["amount"])

# ── Polymarket resolution ────────────────────────────────────────────────────

def fetch_resolution(win_ts_val):
    try:
        slug = f"btc-updown-5m-{win_ts_val}"
        d = _get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        if not d:
            return None
        meta = d[0].get("eventMetadata", {})
        ptb  = meta.get("priceToBeat")
        fp   = meta.get("finalPrice")
        if not ptb or not fp:
            return None
        return {"priceToBeat": float(ptb), "finalPrice": float(fp),
                "actualUp":    float(fp) >= float(ptb)}
    except Exception as e:
        print(f"  Polymarket error: {e}")
        return None

# ── Indicators ───────────────────────────────────────────────────────────────

def calc_rsi(closes, period=9):
    if len(closes) < period + 1:
        return 50.0
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
    if not prices:
        return 0
    k, ema = 2/(period+1), prices[0]
    for p in prices[1:]:
        ema = p*k + ema*(1-k)
    return ema

def rsi_bucket(rsi):
    if rsi < 30:  return "os"
    if rsi < 45:  return "lo"
    if rsi < 55:  return "mi"
    if rsi < 70:  return "hi"
    return "ob"

def features(candles, i):
    if i < 30 or i > len(candles) - 1:
        return None
    hist   = candles[max(0, i-25):i]
    closes = [c["c"] for c in hist]
    last_c = closes[-1]

    p1 = 1 if candles[i-1]["c"] >= candles[i-1]["o"] else 0
    p2 = 1 if candles[i-2]["c"] >= candles[i-2]["o"] else 0
    p3 = 1 if candles[i-3]["c"] >= candles[i-3]["o"] else 0

    rsi       = calc_rsi(closes, 9)
    ema21     = calc_ema(closes, 21)
    vol_avg   = sum(c["v"] for c in hist[-14:]) / 14 if len(hist) >= 14 else 1
    vol_ratio = candles[i-1]["v"] / (vol_avg or 1)

    last_pct = abs((candles[i-1]["c"] - candles[i-1]["o"]) / (candles[i-1]["o"] or 1)) * 100
    if   last_pct >= 0.20: mag = "huge"
    elif last_pct >= 0.10: mag = "big"
    elif last_pct < 0.01:  mag = "noise"
    else:                   mag = "norm"

    last10    = candles[max(0, i-10):i]
    up_ratio  = sum(1 for c in last10 if c["c"] >= c["o"]) / max(len(last10), 1)
    trend     = "sup" if up_ratio >= 0.70 else ("sdn" if up_ratio <= 0.30 else "neu")

    streak = 1
    for j in range(i-2, max(0, i-10), -1):
        if (1 if candles[j]["c"] >= candles[j]["o"] else 0) == p1:
            streak += 1
        else:
            break

    return {"p1": p1, "p2": p2, "p3": p3,
            "rsi": rsi, "rb": rsi_bucket(rsi),
            "ema21": ema21, "ema_trend": 1 if last_c > ema21 else 0,
            "vol_ratio": vol_ratio, "streak": streak,
            "mag": mag, "trend": trend,
            "up_ratio10": up_ratio, "last_pct": last_pct}

def fkey(f):
    return f"{f['p1']}{f['p2']}{f['p3']}_{f['mag']}_{f['trend']}"

def fkey_fb(f):
    return f"{f['p1']}{f['p2']}{f['p3']}_{f['mag']}"

def time_ctx(ts):
    d   = datetime.fromtimestamp(ts, tz=timezone.utc)
    wk  = "wknd" if d.weekday() >= 5 else "wkday"
    s   = ["asia","asia","asia-eu","eu","eu-us","us"][d.hour // 4]
    return f"{wk}_{s}"

# ── Model core ───────────────────────────────────────────────────────────────

def laplace(entry, base):
    a = 2
    return (entry["up"] + a*base) / (entry["total"] + a)

def lookup(tbl, fb_tbl, base, f):
    k = fkey(f); e = tbl.get(k)
    if e and e["total"] >= 10:
        p = laplace(e, base)
        fb = fb_tbl.get(fkey_fb(f))
        if fb and fb["total"] >= 15:
            return p*0.7 + laplace(fb, base)*0.3
        return p
    fb = fb_tbl.get(fkey_fb(f))
    if fb and fb["total"] >= 8:
        return laplace(fb, base)
    return base

def lookup_time(time_tbl, ts):
    e = time_tbl.get(time_ctx(ts))
    if e and e["total"] >= 20:
        return laplace(e, 0.5)
    return 0.5

def combined_prob(tbl, fb_tbl, time_tbl, base, f, ts):
    main = lookup(tbl, fb_tbl, base, f)
    t    = lookup_time(time_tbl, ts)
    direct, dw = base, 0.0
    if   f["mag"] == "huge": direct = 0.80 if f["p1"] else 0.20; dw = 0.45
    elif f["mag"] == "big":  direct = 0.61 if f["p1"] else 0.39; dw = 0.25
    elif f["trend"] == "sup": direct = 0.64; dw = 0.20
    elif f["trend"] == "sdn": direct = 0.36; dw = 0.20
    rw   = 1 - dw
    return max(0.05, min(0.95, direct*dw + main*(rw*0.80) + t*(rw*0.12) + base*(rw*0.08)))

def _tbl_inc(tbl, key, up):
    if key not in tbl:
        tbl[key] = {"up": 0, "total": 0}
    tbl[key]["total"] += 1
    if up:
        tbl[key]["up"] += 1

def train(candles):
    tbl, fb_tbl, time_tbl = {}, {}, {}
    up_cnt = total = 0
    for i in range(30, len(candles) - 1):
        f  = features(candles, i)
        if not f:
            continue
        au = candles[i]["c"] >= candles[i]["o"]
        _tbl_inc(tbl,      fkey(f),    au)
        _tbl_inc(fb_tbl,   fkey_fb(f), au)
        _tbl_inc(time_tbl, time_ctx(candles[i]["t"]), au)
        if au:
            up_cnt += 1
        total += 1
    base = up_cnt / total if total else 0.5

    # Evaluate on last 500 candles
    correct = tested = 0
    for i in range(max(30, len(candles)-500), len(candles)-1):
        f = features(candles, i)
        if not f:
            continue
        p = combined_prob(tbl, fb_tbl, time_tbl, base, f, candles[i]["t"])
        if (p >= 0.5) == (candles[i]["c"] >= candles[i]["o"]):
            correct += 1
        tested += 1

    acc = correct/tested if tested else 0.5
    print(f"  Trained on {total} samples | base={base:.3f} | eval acc={acc:.1%}")
    return {"table": tbl, "fallback": fb_tbl, "timeTable": time_tbl,
            "baseRate": base, "accuracy": acc, "trainedOn": total, "version": 7}

def online_update(mdl, f_snap, actual_up, weight=1.0):
    times = max(1, round(weight))
    for _ in range(times):
        f_like = {"p1": f_snap["p1"], "p2": f_snap["p2"], "p3": f_snap["p3"],
                  "mag": f_snap["mag"], "trend": f_snap["trend"]}
        _tbl_inc(mdl["table"],    fkey(f_like),    actual_up)
        _tbl_inc(mdl["fallback"], fkey_fb(f_like), actual_up)
        ts = f_snap.get("ts", int(time.time()))
        _tbl_inc(mdl["timeTable"], time_ctx(ts), actual_up)

# ── Prediction ───────────────────────────────────────────────────────────────

def make_pred(mdl, candles, ts):
    f = features(candles, len(candles))
    if not f:
        return None
    prob = combined_prob(mdl["table"], mdl["fallback"], mdl["timeTable"],
                         mdl["baseRate"], f, ts)
    neutral   = 0.43 <= prob <= 0.57
    direction = "NEUTRAL" if neutral else ("UP" if prob > 0.5 else "DOWN")
    cs        = abs(prob - 0.5) * 2
    conf      = "גבוה" if cs > 0.30 else ("בינוני" if cs > 0.14 else "נמוך")
    return {
        "winTs":      ts,
        "fKey":       fkey(f),
        "fbKey":      fkey_fb(f),
        "direction":  direction,
        "predictedUp": prob > 0.57,
        "prob":       round(prob, 4),
        "confidence": conf,
        "signals": {
            "mag":       f["mag"],
            "trend":     f["trend"],
            "p1":        f["p1"],
            "p2":        f["p2"],
            "p3":        f["p3"],
            "rsi":       round(f["rsi"], 1),
            "lastPct":   round(f["last_pct"], 3),
            "upRatio10": round(f["up_ratio10"], 2),
            "volRatio":  round(f["vol_ratio"], 2)
        },
        "ts": ts
    }

# ── State I/O ─────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None

def save_state(mdl, pending):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"model": mdl, "pendingPred": pending,
                                       "savedAt": int(time.time())}, ensure_ascii=False))

def load_stats():
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text())
    return {"total": 0, "correct": 0,
            "last200": [],
            "errorPatterns": {"by_mag": {}, "by_trend": {}, "by_session": {}, "by_key": {}},
            "recentLog": []}

def record_outcome(stats, pred, actual_up):
    sigs      = pred.get("signals", {})
    pred_up   = pred.get("predictedUp", True)
    neutral   = pred.get("direction") == "NEUTRAL"
    correct   = (pred_up == actual_up) or neutral

    stats["total"]   += 1
    if correct:
        stats["correct"] += 1
    stats["last200"].append(1 if correct else 0)
    if len(stats["last200"]) > 200:
        stats["last200"].pop(0)

    def pat(cat, key):
        if key not in stats["errorPatterns"][cat]:
            stats["errorPatterns"][cat][key] = {"total": 0, "wrong": 0}
        stats["errorPatterns"][cat][key]["total"] += 1
        if not correct:
            stats["errorPatterns"][cat][key]["wrong"] += 1

    pat("by_mag",     sigs.get("mag",   "?"))
    pat("by_trend",   sigs.get("trend", "?"))
    pat("by_session", time_ctx(pred.get("winTs", 0)))
    pat("by_key",     pred.get("fKey",  "?"))

    ts    = pred.get("winTs", 0)
    label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d %H:%M") if ts else "?"
    entry = {"winTs": ts, "time": label,
             "direction": pred["direction"], "prob": pred["prob"],
             "actualUp": actual_up, "correct": correct,
             "mag": sigs.get("mag"), "trend": sigs.get("trend")}
    stats["recentLog"].insert(0, entry)
    if len(stats["recentLog"]) > 100:
        stats["recentLog"].pop()

    return correct

def save_stats(stats, current_pred):
    STATE_DIR.mkdir(exist_ok=True)
    total   = stats["total"]
    correct = stats["correct"]
    last200 = stats["last200"]
    last20  = last200[-20:] if len(last200) >= 20 else last200
    out = {
        **stats,
        "accuracy":    round(correct / total, 4) if total else 0,
        "accuracy20":  round(sum(last20) / len(last20), 4) if last20 else 0,
        "accuracy200": round(sum(last200) / len(last200), 4) if last200 else 0,
        "currentPrediction": current_pred,
        "updatedAt":   int(time.time()),
    }
    STATS_FILE.write_text(json.dumps(out, ensure_ascii=False))
    acc = out["accuracy"]
    print(f"  Stats: {correct}/{total} = {acc:.1%} | recent200={out['accuracy200']:.1%}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now     = int(time.time())
    cur_win = (now // WIN) * WIN
    utc_now = datetime.fromtimestamp(now, tz=timezone.utc)
    utc_win = datetime.fromtimestamp(cur_win, tz=timezone.utc)

    print(f"\n{'='*56}")
    print(f"BTC Worker  {utc_now:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Window: {cur_win}  ({utc_win:%H:%M} UTC — closes {utc_win.strftime('%H:%M')} + 5m)")
    print(f"{'='*56}")

    state = load_state()
    stats = load_stats()

    if state is None:
        print("\n[INIT] First run — building model from scratch...")
        candles = fetch_training_candles()
        print(f"  Total candles fetched: {len(candles)}")
        mdl     = train(candles)
        pending = None
    else:
        mdl     = state["model"]
        pending = state.get("pendingPred")
        n_keys  = len(mdl.get("table", {}))
        print(f"\n[LOADED] {n_keys} feature keys, {mdl['trainedOn']} trained, "
              f"stats={stats['correct']}/{stats['total']}")
        if pending:
            print(f"[PENDING] win={pending['winTs']} dir={pending['direction']} "
                  f"prob={pending['prob']:.3f}")

    # ── Try to resolve pending prediction ──────────────────────────────────
    if pending:
        prev_win = pending["winTs"]
        if now >= prev_win + WIN + 45:
            print(f"\n[LEARN] Checking resolution for window {prev_win} "
                  f"({datetime.fromtimestamp(prev_win, tz=timezone.utc):%H:%M} UTC)...")
            res = fetch_resolution(prev_win)
            if res:
                actual_up = res["actualUp"]
                pred_up   = pending.get("predictedUp", True)
                neutral   = pending.get("direction") == "NEUTRAL"
                correct   = (pred_up == actual_up) or neutral
                weight    = 1.0 if correct else 3.0

                sigs = pending.get("signals", {})
                f_snap = {"p1": sigs.get("p1", 0), "p2": sigs.get("p2", 0),
                          "p3": sigs.get("p3", 0), "mag": sigs.get("mag", "norm"),
                          "trend": sigs.get("trend", "neu"), "ts": prev_win}
                online_update(mdl, f_snap, actual_up, weight=weight)
                was_ok = record_outcome(stats, pending, actual_up)
                emoji  = "✅" if was_ok else f"❌ (×{weight:.0f} weight)"
                print(f"  PTB={res['priceToBeat']:.2f}  Final={res['finalPrice']:.2f}  "
                      f"Up={actual_up}  {emoji}")
                pending = None
            else:
                print(f"  Not yet resolved or API unavailable — keeping pending")

    # ── Fetch recent candles ───────────────────────────────────────────────
    print("\n[CANDLES] Fetching 200 recent 5-min candles...")
    candles = fetch_candles(limit=200)
    if not candles:
        print("ERROR: No candles returned from Bitstamp")
        sys.exit(1)
    latest = datetime.fromtimestamp(candles[-1]["t"], tz=timezone.utc)
    print(f"  {len(candles)} candles, latest bar: {latest:%H:%M:%S} UTC")

    # ── Make new prediction ────────────────────────────────────────────────
    print(f"\n[PREDICT] Window {cur_win}...")
    pred = make_pred(mdl, candles, cur_win)
    if pred:
        sigs = pred["signals"]
        print(f"  → {pred['direction']}  prob={pred['prob']:.3f}  conf={pred['confidence']}")
        print(f"     mag={sigs['mag']}  trend={sigs['trend']}  "
              f"rsi={sigs['rsi']}  p1={sigs['p1']}")
        pending = pred
    else:
        print("  Could not generate prediction (too few candles)")

    # ── Fetch current price ────────────────────────────────────────────────
    print("\n[PRICE] Fetching Chainlink price...")
    try:
        price = fetch_price()
        print(f"  BTC/USD = ${price:,.2f}")
        if pred:
            pred["currentPrice"] = round(price, 2)
    except Exception as e:
        print(f"  Price fetch failed: {e}")

    # ── Persist ────────────────────────────────────────────────────────────
    save_state(mdl, pending)
    save_stats(stats, pred)

    acc = stats["correct"] / stats["total"] if stats["total"] else 0
    print(f"\n[DONE] Accuracy: {stats['correct']}/{stats['total']} = {acc:.1%}  "
          f"(target: 70%)")
    print(f"Next run in ~5 minutes via GitHub Actions cron")

if __name__ == "__main__":
    main()
