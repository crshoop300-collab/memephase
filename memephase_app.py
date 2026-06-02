#!/usr/bin/env python3
"""
MemePhase v3 - Meme Coin Lifecycle Tracker
Fixes: OHLCV via GeckoTerminal (free, no key), stage tab timing bug
"""

from flask import Flask, render_template_string, jsonify, request
import requests, time

app = Flask(__name__)

DEXSCREENER_BASE  = "https://api.dexscreener.com"
COINGECKO_BASE    = "https://api.coingecko.com/api/v3"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
STAGE_CACHE = {"date": None, "data": None}

# Chain ID mapping dexscreener -> geckoterminal network slug
CHAIN_MAP = {
    "solana":"solana","ethereum":"eth","bsc":"bsc","base":"base",
    "arbitrum":"arbitrum","polygon":"polygon","avalanche":"avax",
    "pulsechain":"pulsechain","optimism":"optimism",
}

def fetch_dex_search(query):
    try:
        r = requests.get(f"{DEXSCREENER_BASE}/latest/dex/search?q={query}", timeout=8)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if not pairs: return None
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd",0) or 0), reverse=True)
        return pairs[0]
    except Exception as e:
        print(f"DexSearch err: {e}"); return None

def fetch_dex_json(path):
    try:
        r = requests.get(f"{DEXSCREENER_BASE}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Dex endpoint err {path}: {e}"); return None

def fetch_token_pairs(chain_id, token_address):
    if not chain_id or not token_address:
        return []
    raw = fetch_dex_json(f"/token-pairs/v1/{chain_id}/{token_address}") or []
    pairs = raw if isinstance(raw, list) else raw.get("pairs", [])
    pairs = [p for p in pairs if p and p.get("pairAddress")]
    pairs.sort(key=lambda p: (
        safe_float((p.get("liquidity") or {}).get("usd")),
        safe_float((p.get("volume") or {}).get("h24")),
    ), reverse=True)
    return pairs

def serialize_analysis(pair, cg_data=None, cg_id=None):
    pair = merge_coingecko_market_data(pair, cg_data, cg_id)
    lc = compute_lifecycle(pair, cg_data)
    cp = {k:pair.get(k) for k in ["baseToken","quoteToken","chainId","dexId","pairAddress",
          "priceUsd","priceChange","volume","liquidity","marketCap","fdv","txns","pairCreatedAt",
          "cgId","marketDataSource","chartSource","url"]}
    return {"pair": cp, "lifecycle": lc}

def discover_stage_tokens(force=False):
    today = time.strftime("%Y-%m-%d")
    if not force and STAGE_CACHE["date"] == today and STAGE_CACHE["data"]:
        return STAGE_CACHE["data"]

    sources = []
    for path in ["/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"]:
        raw = fetch_dex_json(path) or []
        if isinstance(raw, dict):
            raw = [raw]
        sources.extend(raw[:40])

    seen_tokens, candidates = set(), []
    for item in sources:
        chain = item.get("chainId")
        token = item.get("tokenAddress")
        if not chain or not token:
            continue
        key = f"{chain}:{token}".lower()
        if key in seen_tokens:
            continue
        seen_tokens.add(key)
        candidates.append((chain, token))
        if len(candidates) >= 120:
            break

    stages = {"sprout": [], "expansion": [], "peak": [], "cooling": []}
    seen_pairs = set()
    for chain, token in candidates:
        if all(len(v) >= 10 for v in stages.values()):
            break
        pairs = fetch_token_pairs(chain, token)
        if not pairs:
            continue
        pair = pairs[0]
        pair_key = f"{pair.get('chainId')}:{pair.get('pairAddress')}".lower()
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        analysis = serialize_analysis(pair)
        stage_key = {0:"sprout", 1:"sprout", 2:"expansion", 3:"peak", 4:"cooling"}.get(analysis["lifecycle"]["stage_id"])
        if stage_key and len(stages[stage_key]) < 10:
            stages[stage_key].append(analysis)

    payload = {"generated_at": int(time.time()), "stages": stages}
    STAGE_CACHE.update({"date": today, "data": payload})
    return payload

def fetch_geckoterminal_ohlcv(network, pool_address, timeframe="minute", aggregate=15, limit=300):
    """
    GeckoTerminal free OHLCV endpoint - no key needed.
    timeframe: minute | hour | day
    aggregate: candle size (15 for 15m, 60 for 1H, etc.)
    """
    url = f"{GECKOTERMINAL_BASE}/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}"
    params = {"aggregate": aggregate, "limit": limit, "currency": "usd", "token": "base"}
    try:
        r = requests.get(url, headers={"Accept": "application/json;version=20230302"}, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        ohlcv_list = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        candles = []
        for c in ohlcv_list:
            # Format: [timestamp, open, high, low, close, volume]
            ts = int(c[0])
            candles.append({"time": ts, "open": float(c[1]), "high": float(c[2]),
                            "low": float(c[3]), "close": float(c[4])})
        candles.sort(key=lambda x: x["time"])
        # Remove duplicate timestamps (GT sometimes returns dupes)
        seen, deduped = set(), []
        for c in candles:
            if c["time"] not in seen and not (c["open"]==0 and c["close"]==0):
                seen.add(c["time"]); deduped.append(c)
        return deduped
    except Exception as e:
        print(f"GeckoTerminal OHLCV err: {e}"); return []

def fetch_geckoterminal_ohlcv_v2(network, pool_address, timeframe="minute", aggregate=15, limit=300):
    """Alternate GeckoTerminal URL format (some chains use different slug)."""
    # Try the onchain CoinGecko endpoint (same data, slightly different path)
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}"
    params = {"aggregate": str(aggregate), "limit": str(limit), "currency": "usd", "token": "base"}
    try:
        r = requests.get(url, params=params, timeout=10)
        if not r.ok:
            print(f"GT v2 {r.status_code}: {r.text[:200]}")
            return []
        raw = r.json()
        ohlcv_list = (raw.get("data") or {}).get("attributes", {}).get("ohlcv_list", [])
        candles = []
        for c in ohlcv_list:
            ts = int(c[0])
            o,h,l,cl = float(c[1]),float(c[2]),float(c[3]),float(c[4])
            if h==0 and l==0 and o==0 and cl==0: continue
            candles.append({"time":ts,"open":o,"high":h,"low":l,"close":cl})
        candles.sort(key=lambda x: x["time"])
        # Remove duplicate timestamps
        seen, deduped = set(), []
        for c in candles:
            if c["time"] not in seen:
                seen.add(c["time"]); deduped.append(c)
        return deduped
    except Exception as e:
        print(f"GT v2 err: {e}"); return []

def fetch_trending_coingecko():
    try:
        r = requests.get(f"{COINGECKO_BASE}/search/trending", timeout=8)
        r.raise_for_status()
        return r.json().get("coins", [])[:12]
    except Exception as e:
        print(f"CG trending err: {e}"); return []

def fetch_coingecko_coin(coin_id):
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/{coin_id}",
            params={"localization":"false","tickers":"false","market_data":"true",
                    "community_data":"true","developer_data":"false","sparkline":"false"},
            timeout=10)
        r.raise_for_status()
        return r.json()
    except: return None

def fetch_coingecko_match(query, pair=None):
    try:
        r = requests.get(f"{COINGECKO_BASE}/search", params={"query": query}, timeout=8)
        r.raise_for_status()
        coins = r.json().get("coins", [])
        if not coins: return None
        q = (query or "").strip().lower()
        sym = ((pair or {}).get("baseToken") or {}).get("symbol", "").strip().lower()
        for c in coins:
            if c.get("id", "").lower() == q or c.get("name", "").lower() == q:
                return c
        for c in coins:
            if c.get("symbol", "").lower() in {q, sym}:
                return c
        return coins[0]
    except Exception as e:
        print(f"CG search err: {e}"); return None

def interval_seconds(interval_key):
    return {"5m":300, "15m":900, "1H":3600, "4H":14400, "1D":86400}.get(interval_key, 3600)

def coingecko_history_days(interval_key):
    return {"5m":"1", "15m":"1", "1H":"7", "4H":"30", "1D":"365"}.get(interval_key, "30")

def coingecko_ohlc_candles(coin_id, interval_key=None):
    days = coingecko_history_days(interval_key)
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/{coin_id}/ohlc",
                         params={"vs_currency": "usd", "days": days}, timeout=10)
        r.raise_for_status()
        candles = []
        for c in r.json():
            ts = int(c[0] // 1000)
            candles.append({"time": ts, "open": float(c[1]), "high": float(c[2]),
                            "low": float(c[3]), "close": float(c[4])})
        return candles
    except Exception as e:
        print(f"CG OHLC err: {e}"); return []

def coingecko_price_points(coin_id, timeframe="hour", aggregate=4, interval_key=None):
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": coingecko_history_days(interval_key)}
    if interval_key == "1D":
        params["interval"] = "daily"
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        prices = r.json().get("prices", [])
        points, seen = [], set()
        step = max(1, int(aggregate or 1)) if timeframe == "hour" else 1
        last_bucket = None
        for ts_ms, price in prices:
            ts = int(ts_ms // 1000)
            if timeframe == "hour":
                bucket = ts // (step * 3600)
                if bucket == last_bucket:
                    continue
                last_bucket = bucket
            if ts not in seen:
                seen.add(ts)
                points.append({"time": ts, "value": float(price)})
        return points
    except Exception as e:
        print(f"CG chart err: {e}"); return []

def price_points_to_candles(points, interval_key=None):
    if not points:
        return []
    bucket_seconds = interval_seconds(interval_key)
    buckets = {}
    for p in points:
        bucket = int(p["time"] // bucket_seconds) * bucket_seconds
        buckets.setdefault(bucket, []).append(p)
    candles = []
    prev_close = None
    for bucket in sorted(buckets):
        vals = buckets[bucket]
        prices = [float(v["value"]) for v in vals]
        open_price = prev_close if len(prices) == 1 and prev_close is not None else prices[0]
        close_price = prices[-1]
        high_price = max(max(prices), open_price, close_price)
        low_price = min(min(prices), open_price, close_price)
        candles.append({"time": bucket, "open": open_price, "high": high_price,
                        "low": low_price, "close": close_price})
        prev_close = close_price
    return candles

def coingecko_candles(coin_id, timeframe="hour", aggregate=4, interval_key=None):
    candles = price_points_to_candles(coingecko_price_points(coin_id, timeframe, aggregate, interval_key), interval_key)
    if candles:
        return candles
    return coingecko_ohlc_candles(coin_id, interval_key)

def dexscreener_embed_url(chain, pair_addr, interval_key="15m"):
    if not chain or not pair_addr:
        return None
    interval = {"5m":"5", "15m":"15", "1H":"60", "4H":"240", "1D":"D"}.get(interval_key, "15")
    params = (
        "embed=1&loadChartSettings=0&chartLeftToolbar=0"
        f"&chartTheme=dark&theme=dark&chartStyle=1&chartType=usd&interval={interval}"
    )
    return f"https://dexscreener.com/{chain}/{pair_addr}?{params}"


def merge_coingecko_market_data(pair, cg_data, cg_id=None):
    if not cg_data:
        return pair
    md = cg_data.get("market_data") or {}
    p = dict(pair)
    p["cgId"] = cg_id or cg_data.get("id")
    p["chartSource"] = "geckoterminal"
    used_cg = False

    price = (md.get("current_price") or {}).get("usd")
    if price:
        p["priceUsd"] = str(price)
        used_cg = True

    pc24 = md.get("price_change_percentage_24h")
    price_change = dict(p.get("priceChange") or {})
    if pc24 is not None and (not price_change.get("h24") or abs(safe_float(price_change.get("h24"))) < 0.0001):
        price_change["h24"] = pc24
        used_cg = True
    p["priceChange"] = price_change

    volume = dict(p.get("volume") or {})
    cg_vol = (md.get("total_volume") or {}).get("usd")
    if cg_vol and safe_float(volume.get("h24")) < 1000:
        volume["h24"] = cg_vol
        used_cg = True
    p["volume"] = volume

    market_cap = (md.get("market_cap") or {}).get("usd")
    if market_cap and not safe_float(p.get("marketCap")):
        p["marketCap"] = market_cap
        used_cg = True
    elif market_cap and safe_float(volume.get("h24")) >= 1000 and safe_float(p.get("volume", {}).get("h24")) == safe_float(cg_vol):
        p["marketCap"] = market_cap

    if used_cg:
        p["marketDataSource"] = "coingecko"
        p["chartSource"] = "coingecko"
    return p

def safe_float(val, default=0.0):
    try: return float(val) if val is not None else default
    except: return default

def compute_lifecycle(pair, cg_data=None):
    scores, details = {}, {}
    pc1h  = safe_float((pair.get("priceChange") or {}).get("h1"))
    pc6h  = safe_float((pair.get("priceChange") or {}).get("h6"))
    pc24h = safe_float((pair.get("priceChange") or {}).get("h24"))
    mr = pc24h*0.4 + pc6h*0.35 + pc1h*0.25
    s1 = 95 if mr>200 else 85 if mr>100 else 75 if mr>50 else 65 if mr>20 else 50 if mr>5 else 35 if mr>-10 else 20 if mr>-30 else 8
    scores["price_momentum"] = s1
    details["price_momentum"] = f"24h: {pc24h:+.1f}% | 6h: {pc6h:+.1f}% | 1h: {pc1h:+.1f}%"
    vol = safe_float((pair.get("volume") or {}).get("h24"))
    mc  = safe_float(pair.get("marketCap")) or safe_float(pair.get("fdv"))
    vr  = (vol/mc) if mc>0 else 0
    s2 = 90 if vr>3 else 78 if vr>1.5 else 65 if vr>0.8 else 52 if vr>0.4 else 38 if vr>0.15 else 22 if vr>0.05 else 8
    scores["volume_health"] = s2
    details["volume_health"] = f"Vol: ${vol:,.0f} | MCap: ${mc:,.0f} | Ratio: {vr:.2f}x"
    liq = safe_float((pair.get("liquidity") or {}).get("usd"))
    s3 = 75 if liq>5e6 else 80 if liq>1e6 else 85 if liq>500e3 else 70 if liq>100e3 else 55 if liq>25e3 else 38 if liq>5e3 else 15
    scores["liquidity_depth"] = s3
    details["liquidity_depth"] = f"Liquidity: ${liq:,.0f}"
    b1  = safe_float(((pair.get("txns") or {}).get("h1") or {}).get("buys"))
    s1t = safe_float(((pair.get("txns") or {}).get("h1") or {}).get("sells"))
    tot = b1+s1t; bp = (b1/tot) if tot>0 else 0.5
    s4 = 88 if tot>1000 and bp>0.6 else 75 if tot>500 and bp>0.5 else 62 if tot>200 else 48 if tot>50 else 32 if tot>10 else 12
    scores["tx_velocity"] = s4
    details["tx_velocity"] = f"1h Buys: {int(b1)} | 1h Sells: {int(s1t)} | Buy pressure: {bp:.0%}"
    if cg_data:
        cd = cg_data.get("community_data") or {}
        tw = safe_float(cd.get("twitter_followers")); su = safe_float(cg_data.get("sentiment_votes_up_percentage"))
        s5 = 70 if tw>500e3 else 80 if tw>100e3 else 72 if tw>25e3 else 58 if tw>5e3 else 42 if tw>1e3 else 25
        if su>80: s5=min(100,s5+10)
        elif su<40: s5=max(0,s5-10)
        details["social_buzz"] = f"Twitter: {int(tw):,} | Sentiment: {su:.0f}% bullish"
    else:
        s5 = int(s1*0.6+s2*0.4); details["social_buzz"] = "Estimated from on-chain signals"
    scores["social_buzz"] = int(s5)
    weights = {"price_momentum":0.30,"volume_health":0.25,"liquidity_depth":0.15,"tx_velocity":0.20,"social_buzz":0.10}
    composite = sum(scores[k]*weights[k] for k in weights)
    age_h = None
    pca = pair.get("pairCreatedAt")
    if pca: age_h = (time.time() - pca/1000) / 3600
    if age_h and age_h<6:    stage,si,col = "🥚 Launch",0,"#9b59b6";   desc="Brand new token. Extreme risk."
    elif composite>=72:       stage,si,col = "🌱 Sprout",1,"#27ae60";   desc="Strong early momentum. High risk, high upside."
    elif composite>=58:       stage,si,col = "🚀 Expansion",2,"#2ecc71"; desc="Narrative spreading. Sweet spot for risk/reward."
    elif composite>=44:       stage,si,col = "📈 Peak Hype",3,"#f39c12"; desc="Volume peaking. Elevated FOMO risk."
    elif composite>=28:       stage,si,col = "📉 Cooling",4,"#e67e22";   desc="Momentum fading. Liquidity thinning."
    else:                     stage,si,col = "💀 Decline",5,"#e74c3c";   desc="Token dying. Likely distributing."
    fomo = min(100, int(si*18 + max(0,scores["social_buzz"]-50)*0.4 + max(0,70-s1)*0.2))
    rug,rcol = ("🔴 High","#e74c3c") if liq<10e3 else ("🟡 Medium","#f39c12") if liq<50e3 else ("🟢 Low-Mod","#27ae60")
    age_str = (f"{age_h:.1f}h" if age_h and age_h<72 else f"{age_h/24:.1f}d" if age_h else "Unknown")
    return {"stage":stage,"stage_id":si,"stage_desc":desc,"stage_color":col,
            "composite_score":round(composite,1),"fomo_risk":fomo,
            "rug_risk":rug,"rug_color":rcol,"scores":scores,"details":details,
            "age":age_str,"age_hours":age_h}

HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MemePhase — Meme Coin Lifecycle Tracker</title>
<style>
:root,[data-theme="dark"]{
  --bg:#0d0d1a;--card:#14142b;--card2:#1a1a30;--border:#2a2a4a;
  --text:#e8e8f0;--sub:#8888aa;--faint:#333355;
  --accent:#7c3aed;--accent2:#06b6d4;
  --green:#10b981;--red:#ef4444;--yellow:#f59e0b;
  --radius:12px;--trans:180ms cubic-bezier(.16,1,.3,1);
}
[data-theme="light"]{
  --bg:#f4f4f8;--card:#ffffff;--card2:#f0f0f6;--border:#dddde8;
  --text:#1a1a2e;--sub:#666688;--faint:#ddddf0;
  --accent:#6d28d9;--accent2:#0891b2;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;font-size:14px}
header{background:linear-gradient(135deg,#1a0533,#0d1a33);padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.logo{font-size:22px;font-weight:800;background:linear-gradient(90deg,#a855f7,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.tagline{color:var(--sub);font-size:12px}
.theme-btn{background:var(--card);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:8px;cursor:pointer;font-size:16px}
.search-bar{background:var(--card);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.search-bar input{flex:1;min-width:200px;background:var(--card2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;outline:none;transition:border-color var(--trans)}
.search-bar input:focus{border-color:var(--accent)}
.chain-select{background:var(--card2);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:8px;font-size:13px}
.btn-primary{background:var(--accent);color:#fff;border:none;padding:8px 16px;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px;transition:background var(--trans)}
.btn-primary:hover{background:#6d28d9}
.btn-ghost{background:var(--card2);border:1px solid var(--border);color:var(--text);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px}
.btn-ghost:hover{background:var(--faint)}
.tab-nav{background:var(--card);border-bottom:1px solid var(--border);padding:0 20px;display:flex;gap:0;overflow-x:auto;scrollbar-width:none}
.tab-nav::-webkit-scrollbar{display:none}
.tab-btn{padding:12px 18px;border:none;background:none;color:var(--sub);font-size:13px;font-weight:500;cursor:pointer;border-bottom:2px solid transparent;transition:all var(--trans);white-space:nowrap}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--accent2);border-bottom-color:var(--accent2);font-weight:600}
.tab-pane{display:none;padding:20px;max-width:1400px;margin:0 auto}
.tab-pane.active{display:block}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.analysis-grid{grid-template-columns:minmax(280px,1fr) minmax(380px,1fr);align-items:stretch}
.analysis-grid .card,.analysis-grid .chart-container{min-width:0}
.token-summary{order:1}
.lifecycle-card{order:2}
.fomo-card{order:3}
.market-card{order:4}
.breakdown-card{order:5}
.analysis-grid>.chart-container{order:6}
@media(max-width:900px){.grid,.analysis-grid{grid-template-columns:1fr}}
.full{grid-column:1/-1}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px}
.card-title{font-size:11px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.8px;margin-bottom:10px}
.stage-badge{display:inline-block;padding:6px 14px;border-radius:50px;font-size:16px;font-weight:700;margin:2px 0 6px}
.stage-desc{font-size:12px;color:var(--sub);margin-top:2px}
.lifecycle-bar{display:flex;border-radius:8px;overflow:hidden;height:22px;margin:12px 0 0}
.lc-seg{flex:1;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;opacity:.2;transition:opacity .3s}
.lc-seg.active{opacity:1;box-shadow:0 0 8px rgba(255,255,255,.2)}
.score-meter{margin:8px 0}
.meter-label{display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-bottom:3px}
.meter-bar{height:6px;background:var(--card2);border-radius:3px;overflow:hidden}
.meter-fill{height:100%;border-radius:3px;transition:width .8s ease}
.score-detail{font-size:10px;color:var(--sub);margin-top:1px;opacity:.7}
.fomo-number{font-size:48px;font-weight:900;line-height:1;text-align:center}
.fomo-label{font-size:11px;color:var(--sub);text-align:center;margin-top:3px}
.fomo-desc{font-size:12px;margin-top:8px;text-align:center;padding-top:10px;border-top:1px solid var(--border)}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.info-item .lbl{font-size:10px;color:var(--sub);margin-bottom:1px}
.info-item .val{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.positive{color:var(--green)}.negative{color:var(--red)}
.chart-container{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px}
.chart-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px}
.chart-title{font-size:13px;font-weight:600}
.timeframe-btns{display:flex;gap:4px}
.tf-btn{background:var(--card2);border:1px solid var(--border);color:var(--sub);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:500;transition:all var(--trans)}
.tf-btn:hover{color:var(--text)}
.tf-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
#priceChart{width:100%;height:520px;border-radius:8px;overflow:hidden;background:var(--card2)}
.dex-embed{width:100%;height:100%;border:0;background:var(--card2)}
.tv-chart{width:100%;height:100%}
.tv-chart .tradingview-widget-container{height:100%;width:100%}
.tv-chart .tradingview-widget-container__widget{height:100%;width:100%}
.chart-note{font-size:10px;color:var(--sub);margin-top:6px;text-align:center}
.trending-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.trending-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;cursor:pointer;transition:all var(--trans);display:flex;align-items:center;justify-content:space-between;gap:10px}
.trending-card:hover{border-color:var(--accent);background:var(--card2);transform:translateY(-1px);box-shadow:0 4px 20px rgba(124,58,237,.15)}
.t-rank{font-size:18px;font-weight:800;color:var(--sub);min-width:28px}
.t-info .t-name{font-size:14px;font-weight:700}
.t-info .t-sym{font-size:11px;color:var(--sub)}
.t-right{text-align:right}
.t-change{font-size:13px;font-weight:600}
.t-analyze{font-size:10px;color:var(--accent2);margin-top:3px}
.stage-header{display:flex;align-items:center;gap:10px;margin-bottom:14px;padding:10px 14px;background:var(--card);border-radius:var(--radius);border:1px solid var(--border)}
.stage-header-icon{font-size:22px}
.stage-header-title{font-size:15px;font-weight:700}
.stage-header-desc{font-size:11px;color:var(--sub)}
.mini-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:12px;cursor:pointer;transition:all var(--trans)}
.mini-card:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 4px 16px rgba(124,58,237,.12)}
.mini-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px}
.mini-name{font-size:13px;font-weight:700}
.mini-sym{font-size:10px;color:var(--sub)}
.mini-score{font-size:22px;font-weight:900}
.mini-bar{display:flex;height:4px;border-radius:2px;overflow:hidden;gap:1px}
.mini-seg{flex:1;opacity:.2;border-radius:1px}
.mini-seg.active{opacity:1}
.mini-stats{display:flex;justify-content:space-between;font-size:10px;color:var(--sub);margin-top:6px}
.empty{text-align:center;padding:48px 20px;color:var(--sub)}
.empty-icon{font-size:40px;margin-bottom:10px}
.loading{text-align:center;padding:40px;color:var(--sub)}
.spinner{display:inline-block;width:26px;height:26px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;margin-bottom:8px}
@keyframes spin{to{transform:rotate(360deg)}}
.alert{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);border-radius:8px;padding:8px 12px;font-size:11px;color:var(--yellow);margin-top:10px}
.token-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.token-summary{min-height:208px;display:flex;align-items:center}
.token-summary .token-header{width:100%}
.t-name-big{font-size:20px;font-weight:800}
.t-meta{font-size:11px;color:var(--sub);margin-top:2px}
.price-usd{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}
.price-change{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums}
.badge-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;background:var(--card2);border:1px solid var(--border);color:var(--sub)}
</style>
</head>
<body>

<header>
  <div>
    <div class="logo">🪙 MemePhase</div>
    <div class="tagline">Meme Coin Lifecycle Tracker — Know Where You Are in the Cycle</div>
  </div>
  <button class="theme-btn" id="themeBtn" title="Toggle theme">☀️</button>
</header>

<div class="search-bar">
  <input id="tokenInput" type="text" placeholder="Token symbol or contract address (e.g. PEPE, BONK, WIF)..." onkeydown="if(event.key==='Enter') doAnalyze()" />
  <select class="chain-select" id="chainSelect">
    <option value="solana">Solana</option>
    <option value="ethereum">Ethereum</option>
    <option value="bsc">BSC</option>
    <option value="base">Base</option>
    <option value="arbitrum">Arbitrum</option>
    <option value="polygon">Polygon</option>
  </select>
  <button class="btn-primary" onclick="doAnalyze()">🔍 Analyze</button>
  <button class="btn-ghost" onclick="loadTrending()">🔥 Trending</button>
</div>

<nav class="tab-nav">
  <button class="tab-btn active" data-tab="trending">🔥 Trending</button>
  <button class="tab-btn" data-tab="sprout">🌱 Sprout</button>
  <button class="tab-btn" data-tab="expansion">🚀 Expansion</button>
  <button class="tab-btn" data-tab="peak">📈 Peak Hype</button>
  <button class="tab-btn" data-tab="cooling">📉 Cooling</button>
  <button class="tab-btn" data-tab="analyze">🔍 Analysis</button>
</nav>

<div class="tab-pane active" id="tab-trending">
  <div id="trendingContent"><div class="loading"><div class="spinner"></div><div>Loading trending coins...</div></div></div>
</div>
<div class="tab-pane" id="tab-sprout">
  <div id="sproutContent"><div class="empty"><div class="empty-icon">🌱</div><div>Analyze a token to populate this stage.</div></div></div>
</div>
<div class="tab-pane" id="tab-expansion">
  <div id="expansionContent"><div class="empty"><div class="empty-icon">🚀</div><div>Analyze a token to populate this stage.</div></div></div>
</div>
<div class="tab-pane" id="tab-peak">
  <div id="peakContent"><div class="empty"><div class="empty-icon">📈</div><div>Analyze a token to populate this stage.</div></div></div>
</div>
<div class="tab-pane" id="tab-cooling">
  <div id="coolingContent"><div class="empty"><div class="empty-icon">📉</div><div>Analyze a token to populate this stage.</div></div></div>
</div>
<div class="tab-pane" id="tab-analyze">
  <div id="analyzeContent">
    <div class="empty"><div class="empty-icon">🔍</div><div style="font-size:15px;font-weight:600;margin-bottom:6px">Enter a token above to analyze</div><div style="font-size:12px">Symbol (PEPE, BONK) or paste a contract address</div></div>
  </div>
</div>

<script>
// ── THEME ──────────────────────────────────
const themeBtn = document.getElementById('themeBtn');
let theme = 'dark';
themeBtn.addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', theme);
  themeBtn.textContent = theme === 'dark' ? '☀️' : '🌙';
  if (currentChart) updateChartTheme();
  else if (currentPairAddr) loadChart(currentTF);
});

// ── TABS ───────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});
function switchTab(id) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab===id));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id==='tab-'+id));
}

// ── CHART ──────────────────────────────────
let currentChart = null, currentPairAddr = null, currentChainId = null, currentCgId = null, currentChartSource = null, currentSymbol = null, currentName = null, currentTF = '15m';

const TF_PARAMS = {
  '5m':  {tf:'minute', agg:5,  limit:300, interval:'5m',  visible:60},
  '15m': {tf:'minute', agg:15, limit:300, interval:'15m', visible:80},
  '1H':  {tf:'hour',   agg:1,  limit:300, interval:'1H',  visible:100},
  '4H':  {tf:'hour',   agg:4,  limit:300, interval:'4H',  visible:100},
  '1D':  {tf:'day',    agg:1,  limit:365, interval:'1D',  visible:90},
};

function chartColors() {
  return theme === 'dark'
    ? {bg:'#14142b', text:'#8888aa', grid:'#2a2a4a', up:'#10b981', dn:'#ef4444'}
    : {bg:'#ffffff', text:'#666688', grid:'#dddde8', up:'#10b981', dn:'#ef4444'};
}

function initChart() {
  const c = chartColors();
  const el = document.getElementById('priceChart');
  if (!el) return null;
  el.innerHTML = '';
  const chart = LightweightCharts.createChart(el, {
    width: el.offsetWidth, height: 320,
    layout: {background:{color:c.bg}, textColor:c.text},
    grid: {vertLines:{color:c.grid}, horzLines:{color:c.grid}},
    crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
    rightPriceScale: {borderColor:c.grid, scaleMargins:{top:0.1,bottom:0.1}},
    timeScale: {
      borderColor:c.grid,
      timeVisible:true,
      secondsVisible:false,
      rightOffset:6,
      barSpacing:12,
      fixLeftEdge:false,
      fixRightEdge:false,
      lockVisibleTimeRangeOnResize:false,
      shiftVisibleRangeOnNewBar:false,
    },
    handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:false},
    handleScale:{axisPressedMouseMove:true,mouseWheel:true,pinch:true},
  });
  const seriesOptions = {
    upColor:c.up, downColor:c.dn, borderUpColor:c.up, borderDownColor:c.dn, wickUpColor:c.up, wickDownColor:c.dn,
  };
  const series = chart.addCandlestickSeries
    ? chart.addCandlestickSeries(seriesOptions)
    : chart.addSeries(LightweightCharts.CandlestickSeries, seriesOptions);
  window.addEventListener('resize', () => {
    const e = document.getElementById('priceChart');
    if (e) chart.applyOptions({width: e.offsetWidth});
  });
  return {chart, series};
}

function updateChartTheme() {
  if (!currentChart) return;
  const c = chartColors();
  currentChart.chart.applyOptions({
    layout:{background:{color:c.bg},textColor:c.text},
    grid:{vertLines:{color:c.grid},horzLines:{color:c.grid}},
    rightPriceScale:{borderColor:c.grid}, timeScale:{borderColor:c.grid},
  });
  currentChart.series.applyOptions({upColor:c.up,downColor:c.dn,borderUpColor:c.up,borderDownColor:c.dn,wickUpColor:c.up,wickDownColor:c.dn});
}

async function loadChart(tfKey) {
  currentTF = tfKey;
  document.querySelectorAll('.tf-btn').forEach(b => b.classList.toggle('active', b.dataset.tf===tfKey));
  if (!currentPairAddr) return;
  const chartEl = document.getElementById('priceChart');
  if (!chartEl) return;
  const p = TF_PARAMS[tfKey];
  try {
    const cgPart = currentCgId ? `&cg=${encodeURIComponent(currentCgId)}` : '';
    const sourcePart = currentChartSource ? `&source=${encodeURIComponent(currentChartSource)}` : '';
    const limitPart = p.limit ? `&limit=${p.limit}` : '';
    const intervalPart = p.interval ? `&interval=${encodeURIComponent(p.interval)}` : '';
    const r = await fetch(`/api/ohlcv?pair=${currentPairAddr}&chain=${currentChainId||'solana'}&tf=${p.tf}&agg=${p.agg}${limitPart}${intervalPart}${cgPart}${sourcePart}`);
    const data = await r.json();
    const candles = data.candles || [];
    if (!candles || candles.length === 0) {
      if (data.embed_url) {
        chartEl.innerHTML = `<iframe class="dex-embed" src="${data.embed_url}" title="DexScreener chart" loading="lazy"></iframe>`;
        currentChart = null; return;
      }
      chartEl.innerHTML = `<div style="padding:40px;text-align:center;color:var(--sub);font-size:13px">
        📊 No chart data for this timeframe.<br>
        <span style="font-size:11px;display:block;margin-top:6px">Try a longer timeframe (1H or 1D) — very new tokens may only have daily data.</span>
        ${data.error ? '<span style="font-size:10px;color:#ef4444;display:block;margin-top:4px">'+data.error+'</span>' : ''}
      </div>`;
      currentChart = null; return;
    }
    if (!currentChart) { currentChart = initChart(); }
    if (!currentChart) return;
    currentChart.series.setData(candles);
    const visibleBars = Math.min(candles.length, p.visible || 80);
    if (candles.length > visibleBars) {
      currentChart.chart.timeScale().setVisibleLogicalRange({from:candles.length-visibleBars, to:candles.length+4});
    } else {
      currentChart.chart.timeScale().fitContent();
      currentChart.chart.timeScale().scrollToPosition(4, false);
    }
  } catch(e) {
    console.error('Chart err:', e);
    if (chartEl) chartEl.innerHTML = '<div style="padding:40px;text-align:center;color:var(--sub)">Chart unavailable.</div>';
  }
}

// ── HELPERS ────────────────────────────────
const EMBED_TF = {
  '5m':  {tv:'5', dex:'5'},
  '15m': {tv:'15', dex:'15'},
  '1H':  {tv:'60', dex:'60'},
  '4H':  {tv:'240', dex:'240'},
  '1D':  {tv:'D', dex:'D'},
};

const TV_SYMBOLS = {
  BTC:'BINANCE:BTCUSDT', WBTC:'BINANCE:BTCUSDT',
  ETH:'BINANCE:ETHUSDT', SOL:'BINANCE:SOLUSDT', BNB:'BINANCE:BNBUSDT',
  XRP:'BINANCE:XRPUSDT', DOGE:'BINANCE:DOGEUSDT', ADA:'BINANCE:ADAUSDT',
  AVAX:'BINANCE:AVAXUSDT', LINK:'BINANCE:LINKUSDT', DOT:'BINANCE:DOTUSDT',
  TRX:'BINANCE:TRXUSDT', TON:'BINANCE:TONUSDT', LTC:'BINANCE:LTCUSDT',
  BCH:'BINANCE:BCHUSDT', UNI:'BINANCE:UNIUSDT', AAVE:'BINANCE:AAVEUSDT',
  SUI:'BINANCE:SUIUSDT', HBAR:'BINANCE:HBARUSDT', XLM:'BINANCE:XLMUSDT',
  FIL:'BINANCE:FILUSDT', NEAR:'BINANCE:NEARUSDT', INJ:'BINANCE:INJUSDT',
  SEI:'BINANCE:SEIUSDT', OP:'BINANCE:OPUSDT', ARB:'BINANCE:ARBUSDT',
  PEPE:'BINANCE:PEPEUSDT', SHIB:'BINANCE:SHIBUSDT', BONK:'BINANCE:BONKUSDT',
  WIF:'BINANCE:WIFUSDT', FLOKI:'BINANCE:FLOKIUSDT'
};

function setChartNote(text) {
  const note = document.getElementById('chartNote');
  if (note) note.textContent = text;
}

function currentTradingViewSymbol() {
  return TV_SYMBOLS[String(currentSymbol||'').toUpperCase()] || null;
}

function renderTradingViewChart(tvSymbol, tfKey) {
  const chartEl = document.getElementById('priceChart');
  if (!chartEl) return;
  const p = EMBED_TF[tfKey] || EMBED_TF['15m'];
  chartEl.innerHTML = '<div class="tv-chart"><div class="tradingview-widget-container"><div class="tradingview-widget-container__widget"></div></div></div>';
  const container = chartEl.querySelector('.tradingview-widget-container');
  const script = document.createElement('script');
  script.type = 'text/javascript';
  script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
  script.async = true;
  script.text = JSON.stringify({
    autosize:true,
    symbol:tvSymbol,
    interval:p.tv,
    timezone:'Etc/UTC',
    theme:theme === 'dark' ? 'dark' : 'light',
    style:'1',
    locale:'en',
    hide_side_toolbar:false,
    hide_top_toolbar:false,
    allow_symbol_change:true,
    save_image:false,
    calendar:false,
    details:false,
    hotlist:false,
    withdateranges:true,
    hide_volume:false,
    backgroundColor:theme === 'dark' ? '#14142b' : '#ffffff',
    gridColor:theme === 'dark' ? 'rgba(136,136,170,0.18)' : 'rgba(102,102,136,0.18)'
  });
  container.appendChild(script);
  currentChart = null;
  setChartNote(`TradingView chart · ${tvSymbol}`);
}

function renderDexScreenerChart(tfKey) {
  const chartEl = document.getElementById('priceChart');
  if (!chartEl || !currentPairAddr || !currentChainId) return;
  const p = EMBED_TF[tfKey] || EMBED_TF['15m'];
  const params = new URLSearchParams({
    embed:'1',
    loadChartSettings:'0',
    chartLeftToolbar:'1',
    chartTheme:theme,
    theme:theme,
    chartStyle:'1',
    chartType:'usd',
    interval:p.dex,
  });
  const src = `https://dexscreener.com/${encodeURIComponent(currentChainId)}/${encodeURIComponent(currentPairAddr)}?${params.toString()}`;
  chartEl.innerHTML = `<iframe class="dex-embed" src="${src}" title="DexScreener chart" loading="lazy"></iframe>`;
  currentChart = null;
  setChartNote('DexScreener chart · exact DEX pair');
}

async function loadChart(tfKey) {
  currentTF = tfKey;
  document.querySelectorAll('.tf-btn').forEach(b => b.classList.toggle('active', b.dataset.tf===tfKey));
  const tvSymbol = currentTradingViewSymbol();
  if (tvSymbol) renderTradingViewChart(tvSymbol, tfKey);
  else renderDexScreenerChart(tfKey);
}

const fmt = n => !n ? '0' : n>1e9?(n/1e9).toFixed(2)+'B':n>1e6?(n/1e6).toFixed(2)+'M':n>1e3?(n/1e3).toFixed(1)+'K':n.toLocaleString();
const fmtP = p => {
  const f = parseFloat(p||0);
  if (!Number.isFinite(f) || f===0) return '—';
  if (Math.abs(f) >= 1) return f.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  return f<0.000001?f.toExponential(4):f<0.01?f.toFixed(8):f.toFixed(5);
};
const jsArg = v => String(v??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,' ');
const scCol = s => s>=70?'#10b981':s>=50?'#f59e0b':'#ef4444';
const fomoCol= s => s>=75?'#ef4444':s>=50?'#f59e0b':s>=25?'#10b981':'#06b6d4';
const STAGES=['🥚 Launch','🌱 Sprout','🚀 Expansion','📈 Peak Hype','📉 Cooling','💀 Decline'];
const SCOLS=['#9b59b6','#27ae60','#2ecc71','#f39c12','#e67e22','#e74c3c'];
const SNAMES={price_momentum:'Price Momentum',volume_health:'Vol/MCap Health',liquidity_depth:'Liquidity Depth',tx_velocity:'Tx Velocity',social_buzz:'Social Buzz'};

function lcBar(ai) { return STAGES.map((s,i)=>`<div class="lc-seg${i===ai?' active':''}" style="background:${SCOLS[i]}">${s.split(' ')[0]}</div>`).join(''); }
function miniBar(ai){ return STAGES.map((s,i)=>`<div class="mini-seg${i===ai?' active':''}" style="background:${SCOLS[i]}"></div>`).join(''); }

// ── STAGE TABS ─────────────────────────────
const STAB = {0:'sprout',1:'sprout',2:'expansion',3:'peak',4:'cooling'};
const stageLists = {sprout:[],expansion:[],peak:[],cooling:[]};
const STAGEINFO = {
  sprout:   {icon:'🌱',title:'Sprout Stage',desc:'Early momentum. High risk, high upside.',col:'#27ae60'},
  expansion:{icon:'🚀',title:'Expansion Stage',desc:'Narrative spreading. Sweet spot.',col:'#2ecc71'},
  peak:     {icon:'📈',title:'Peak Hype Stage',desc:'Volume peaking. Elevated FOMO.',col:'#f39c12'},
  cooling:  {icon:'📉',title:'Cooling Stage',desc:'Momentum fading. Exit liquidity thin.',col:'#e67e22'},
};

function addToStageTab(data) {
  const key = STAB[data.lifecycle.stage_id];
  if (!key) return;
  const sym = data.pair.baseToken.symbol;
  stageLists[key] = stageLists[key].filter(d => d.pair.baseToken.symbol !== sym);
  stageLists[key].unshift(data);
  renderStageTab(key);
}

function renderStageTab(key) {
  const list = stageLists[key];
  const s = STAGEINFO[key];
  const el = document.getElementById(key+'Content');
  if (!el) return;
  if (!list.length) {
    el.innerHTML=`<div class="empty"><div class="empty-icon">${s.icon}</div><div>No fresh ${s.title} coins found yet.</div></div>`;
    return;
  }
  const cards = list.map(d => {
    const p=d.pair, lc=d.lifecycle;
    const pc24=parseFloat((p.priceChange||{}).h24||0);
    const mc=parseInt(p.marketCap||p.fdv||0);
    const q=(p.baseToken||{}).address||p.pairAddress||p.baseToken.symbol;
    return `<div class="mini-card" onclick="analyzeFull('${jsArg(q)}')">
      <div class="mini-top">
        <div><div class="mini-name">${p.baseToken.name}</div><div class="mini-sym">${p.baseToken.symbol} · ${p.chainId}</div></div>
        <div class="mini-score" style="color:${lc.stage_color}">${lc.composite_score}</div>
      </div>
      <div class="mini-bar">${miniBar(lc.stage_id)}</div>
      <div class="mini-stats">
        <span class="${pc24>=0?'positive':'negative'}">${pc24>=0?'+':''}${pc24.toFixed(1)}% 24h</span>
        <span>$${fmt(mc)}</span>
        <span>FOMO ${lc.fomo_risk}</span>
      </div>
    </div>`;
  }).join('');
  el.innerHTML=`<div class="stage-header"><div class="stage-header-icon">${s.icon}</div><div><div class="stage-header-title" style="color:${s.col}">${s.title}</div><div class="stage-header-desc">${s.desc}</div></div></div><div class="trending-grid">${cards}</div>`;
}

// ── ANALYZE ────────────────────────────────
async function loadLifecycleStages(refresh=false) {
  ['sprout','expansion','peak','cooling'].forEach(k => {
    const el=document.getElementById(k+'Content');
    if (el) el.innerHTML='<div class="loading"><div class="spinner"></div><div>Finding fresh coins...</div></div>';
  });
  try {
    const data = await fetch(`/api/stages${refresh?'?refresh=1':''}`).then(r=>r.json());
    ['sprout','expansion','peak','cooling'].forEach(k => {
      stageLists[k] = (data.stages && data.stages[k]) || [];
      renderStageTab(k);
    });
  } catch(e) {
    ['sprout','expansion','peak','cooling'].forEach(k => {
      const s=STAGEINFO[k], el=document.getElementById(k+'Content');
      if (el) el.innerHTML=`<div class="empty"><div class="empty-icon">${s.icon}</div><div style="color:#ef4444">Could not load fresh ${s.title} coins.</div></div>`;
    });
  }
}

async function doAnalyze() {
  const q = document.getElementById('tokenInput').value.trim();
  if (!q) return;
  switchTab('analyze');
  document.getElementById('analyzeContent').innerHTML='<div class="loading"><div class="spinner"></div><div>Fetching on-chain data...</div></div>';
  try {
    const r = await fetch(`/api/analyze?q=${encodeURIComponent(q)}&chain=${document.getElementById('chainSelect').value}`);
    const data = await r.json();
    if (data.error) { showErr(data.error); return; }
    renderAnalysis(data);
    addToStageTab(data);
  } catch(e) { showErr('Network error — is the server running?'); }
}

async function analyzeFull(sym) {
  document.getElementById('tokenInput').value = sym;
  await doAnalyze();
}

function showErr(msg) {
  document.getElementById('analyzeContent').innerHTML=`<div class="empty"><div class="empty-icon">⚠️</div><div style="color:#ef4444">${msg}</div></div>`;
}

function renderAnalysis(d) {
  const p=d.pair, lc=d.lifecycle;
  currentPairAddr = p.pairAddress;
  currentChainId  = p.chainId;
  currentCgId     = p.cgId || null;
  currentChartSource = p.chartSource || null;
  currentSymbol   = (p.baseToken || {}).symbol || null;
  currentName     = (p.baseToken || {}).name || null;
  currentChart    = null;
  const pc24Raw=(p.priceChange||{}).h24;
  const hasPc24=pc24Raw!==undefined && pc24Raw!==null && pc24Raw!=='';
  const pc24=parseFloat(pc24Raw||0);
  const pcClass=pc24>=0?'positive':'negative';
  const pcStr=hasPc24?(pc24>=0?'+':'')+pc24.toFixed(2)+'%':'—';
  const mc=parseInt(p.marketCap||p.fdv||0);
  const vol=parseInt((p.volume||{}).h24||0);
  const liq=parseInt((p.liquidity||{}).usd||0);
  const fomoDesc=lc.fomo_risk>=75?"🔴 <strong>Very High</strong> — Likely too late. Distribution risk.":
                 lc.fomo_risk>=50?"🟡 <strong>Elevated</strong> — Tight stop-loss advised.":
                 lc.fomo_risk>=25?"🟢 <strong>Moderate</strong> — Reasonable if thesis holds.":
                                  "🔵 <strong>Low</strong> — Early stage. Verify legitimacy.";
  const scoresHtml=Object.entries(lc.scores).map(([k,v])=>`
    <div class="score-meter">
      <div class="meter-label"><span>${SNAMES[k]}</span><span style="color:${scCol(v)}">${v}</span></div>
      <div class="meter-bar"><div class="meter-fill" style="width:${v}%;background:${scCol(v)}"></div></div>
      <div class="score-detail">${lc.details[k]}</div>
    </div>`).join('');

  document.getElementById('analyzeContent').innerHTML=`
  <div class="grid analysis-grid">
    <div class="card token-summary">
      <div class="token-header">
        <div>
          <div class="t-name-big">${p.baseToken.name} <span style="color:var(--sub);font-size:14px">${p.baseToken.symbol}</span></div>
          <div class="t-meta">Chain: ${p.chainId} &nbsp;·&nbsp; DEX: ${p.dexId} &nbsp;·&nbsp; Age: ${lc.age}</div>
          <div class="t-meta" style="font-size:10px;margin-top:2px">${p.pairAddress||''}</div>
        </div>
        <div style="text-align:right">
          <div class="price-usd">$${fmtP(p.priceUsd)}</div>
          <div class="price-change ${pcClass}">${pcStr} 24h</div>
        </div>
      </div>
    </div>

    <div class="chart-container full">
      <div class="chart-header">
        <div class="chart-title">📊 Price Chart — ${p.baseToken.symbol} / USD</div>
        <div class="timeframe-btns">
          <button class="tf-btn" data-tf="5m"  onclick="loadChart('5m')">5m</button>
          <button class="tf-btn active" data-tf="15m" onclick="loadChart('15m')">15m</button>
          <button class="tf-btn" data-tf="1H"  onclick="loadChart('1H')">1H</button>
          <button class="tf-btn" data-tf="4H"  onclick="loadChart('4H')">4H</button>
          <button class="tf-btn" data-tf="1D"  onclick="loadChart('1D')">1D</button>
        </div>
      </div>
      <div id="priceChart"></div>
      <div class="chart-note" id="chartNote">Chart loading...</div>
    </div>

    <div class="card lifecycle-card">
      <div class="card-title">Lifecycle Stage</div>
      <div class="stage-badge" style="background:${lc.stage_color}22;color:${lc.stage_color};border:1px solid ${lc.stage_color}44">${lc.stage}</div>
      <div style="font-size:30px;font-weight:900;color:${lc.stage_color}">${lc.composite_score}/100</div>
      <div class="stage-desc">${lc.stage_desc}</div>
      <div class="lifecycle-bar">${lcBar(lc.stage_id)}</div>
    </div>

    <div class="card fomo-card">
      <div class="card-title">FOMO Risk Score</div>
      <div class="fomo-number" style="color:${fomoCol(lc.fomo_risk)}">${lc.fomo_risk}</div>
      <div class="fomo-label">out of 100</div>
      <div class="fomo-desc">${fomoDesc}</div>
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
        <div class="card-title">Rug Risk</div>
        <div style="font-size:16px;font-weight:700;color:${lc.rug_color}">${lc.rug_risk}</div>
        <div style="font-size:10px;color:var(--sub)">Based on liquidity ($${fmt(liq)})</div>
      </div>
    </div>

    <div class="card market-card">
      <div class="card-title">Market Data</div>
      <div class="info-grid">
        <div class="info-item"><div class="lbl">Market Cap</div><div class="val">$${fmt(mc)}</div></div>
        <div class="info-item"><div class="lbl">24h Volume</div><div class="val">$${fmt(vol)}</div></div>
        <div class="info-item"><div class="lbl">Liquidity</div><div class="val">$${fmt(liq)}</div></div>
        <div class="info-item"><div class="lbl">5m Change</div>
          <div class="val ${parseFloat((p.priceChange||{}).m5||0)>=0?'positive':'negative'}">
            ${(parseFloat((p.priceChange||{}).m5||0)>=0?'+':'')}${parseFloat((p.priceChange||{}).m5||0).toFixed(2)}%
          </div>
        </div>
        <div class="info-item"><div class="lbl">1h Buys</div><div class="val positive">${((p.txns||{}).h1||{}).buys||0}</div></div>
        <div class="info-item"><div class="lbl">1h Sells</div><div class="val negative">${((p.txns||{}).h1||{}).sells||0}</div></div>
      </div>
      <div class="alert">⚠️ Not financial advice. Meme coins are extremely high risk. Always DYOR.</div>
    </div>

    <div class="card full breakdown-card">
      <div class="card-title">Lifecycle Signal Breakdown</div>
      ${scoresHtml}
    </div>
  </div>`;

  // Load chart after DOM settles
  requestAnimationFrame(() => requestAnimationFrame(() => loadChart('15m')));
}

// ── TRENDING ───────────────────────────────
async function loadTrending() {
  switchTab('trending');
  document.getElementById('trendingContent').innerHTML='<div class="loading"><div class="spinner"></div><div>Loading trending coins...</div></div>';
  try {
    const coins = await fetch('/api/trending').then(r=>r.json());
    if (!coins||!coins.length) throw new Error('No data');
    const cards = coins.map((c,i) => {
      const item=c.item||c;
      const name=item.name||'?', sym=item.symbol||'?';
      const pc24=item.data?.price_change_percentage_24h?.usd??null;
      const pcStr=pc24!==null?(pc24>=0?`<span class="positive">+${pc24.toFixed(1)}%</span>`:`<span class="negative">${pc24.toFixed(1)}%</span>`):'—';
      const price=item.data?.price?`$${fmtP(item.data.price)}`:'—';
      return `<div class="trending-card" onclick="analyzeFull('${sym}')">
        <div class="t-rank">#${i+1}</div>
        <div class="t-info" style="flex:1"><div class="t-name">${name}</div><div class="t-sym">${sym} · ${price}</div></div>
        <div class="t-right"><div class="t-change">${pcStr}</div><div class="t-analyze">Analyze →</div></div>
      </div>`;
    }).join('');
    document.getElementById('trendingContent').innerHTML=`
      <div style="margin-bottom:12px;font-size:12px;color:var(--sub)">CoinGecko trending — click any coin to run lifecycle analysis</div>
      <div class="trending-grid">${cards}</div>`;
  } catch(e) {
    document.getElementById('trendingContent').innerHTML='<div class="empty"><div class="empty-icon">⚠️</div><div style="color:#ef4444">Could not load trending data.</div></div>';
  }
}

loadLifecycleStages();
loadTrending();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/analyze")
def api_analyze():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error":"No query"}),400
    pair = fetch_dex_search(q)
    if not pair: return jsonify({"error":f"No token found for '{q}'. Try the contract address."}),404
    cg_data, cg_id = None, None
    try:
        cg_match = fetch_coingecko_match(q, pair)
        if cg_match:
            cg_id = cg_match.get("id")
            cg_data = fetch_coingecko_coin(cg_id)
    except: pass
    return jsonify(serialize_analysis(pair, cg_data, cg_id))

@app.route("/api/ohlcv")
def api_ohlcv():
    pair_addr = request.args.get("pair","").strip()
    chain     = request.args.get("chain","solana").strip()
    tf        = request.args.get("tf","minute").strip()
    agg       = request.args.get("agg","15").strip()
    limit     = request.args.get("limit","300").strip()
    interval_key = request.args.get("interval","").strip() or request.args.get("range","").strip()
    cg_id     = request.args.get("cg","").strip()
    source    = request.args.get("source","").strip()
    if not pair_addr: return jsonify({"error":"No pair","candles":[]}),400
    if cg_id and source == "coingecko":
        candles = coingecko_candles(cg_id, tf, agg, interval_key)
        if candles:
            return jsonify({"candles": candles, "source": "coingecko"})
    network = CHAIN_MAP.get(chain, chain)
    # Try GeckoTerminal
    candles = fetch_geckoterminal_ohlcv(network, pair_addr, tf, agg, limit)
    if candles:
        return jsonify({"candles": candles, "source": "geckoterminal"})
    # Fallback: try alternate GT URL format
    candles = fetch_geckoterminal_ohlcv_v2(network, pair_addr, tf, agg, limit)
    if candles:
        return jsonify({"candles": candles, "source": "geckoterminal_v2"})
    if cg_id:
        candles = coingecko_candles(cg_id, tf, agg, interval_key)
        if candles:
            return jsonify({"candles": candles, "source": "coingecko"})
    return jsonify({
        "candles": [],
        "error": "No OHLCV data available from GeckoTerminal or CoinGecko",
        "embed_url": dexscreener_embed_url(chain, pair_addr, interval_key),
        "source": "dexscreener_embed",
    })

@app.route("/api/debug")
def api_debug():
    """Debug endpoint to test GeckoTerminal connectivity."""
    pair_addr = request.args.get("pair","0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640").strip()
    network   = request.args.get("network","eth").strip()
    url1 = f"{GECKOTERMINAL_BASE}/networks/{network}/pools/{pair_addr}/ohlcv/minute"
    results = {}
    try:
        r = requests.get(url1, headers={"Accept":"application/json;version=20230302"},
                         params={"aggregate":15,"limit":10,"currency":"usd","token":"base"}, timeout=10)
        results["gt_v1"] = {"status": r.status_code, "keys": list(r.json().keys()) if r.ok else r.text[:200]}
    except Exception as e:
        results["gt_v1"] = {"error": str(e)}
    return jsonify(results)

@app.route("/api/trending")
def api_trending():
    return jsonify(fetch_trending_coingecko())

@app.route("/api/stages")
def api_stages():
    force = request.args.get("refresh") == "1"
    return jsonify(discover_stage_tokens(force=force))

if __name__ == "__main__":
    import os
    print("\nMemePhase v3 running at http://localhost:5000")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
