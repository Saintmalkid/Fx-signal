#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gold-hl-bot — multi-market High/Low breakout signal bot for Telegram.
Designed to run on GitHub Actions free tier (public repo = free minutes).

Roles (set via env ROLE):
    free  -> FREE channel: GOLD ONLY          (env TELEGRAM_CHAT_ID)
    vip   -> VIP channel:  GOLD + FOREX pairs (env TELEGRAM_VIP_CHAT_ID)
    recap -> daily summary of the last 24h to both channels

VIP symbol rotation: every run scans ALL VIP symbols; each symbol signals
independently — so if gold is quiet, EUR/USD / GBP/USD / USD/JPY keep the
VIP channel alive. Free stays gold-only.

The bot's memory (per-symbol price history, open trades, closed trades) is
stored in state-<role>.json, committed back to the repo by the workflow.
Old single-symbol state files are migrated automatically.

Price data per symbol (first source that works):
    1. Twelve Data (if TWELVEDATA_KEY secret is set)
    2. Yahoo Finance — no key needed
    3. gold-api.com spot — no key (gold only)

If TELEGRAM_BOT_TOKEN is not set, the bot DRY-RUNs: prints messages instead
of sending them (useful for testing).
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# ------------------------------------------------------------------ config ----

ROLE        = os.environ.get("ROLE", "free").strip().lower()
STATE_FILE  = "state-%s.json" % ROLE

TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
VIP_CHAT    = os.environ.get("TELEGRAM_VIP_CHAT_ID", "").strip()
FREE_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TD_KEY      = os.environ.get("TWELVEDATA_KEY", "").strip()
FOOTER      = os.environ.get("FOOTER", "").strip()

# Which symbols each role watches
SYMBOLS_BY_ROLE = {
    "free": ["XAUUSD"],

    # Optimized VIP list:
    # GOLD + all 7 major forex pairs + 10 liquid cross pairs.
    # No exotic pairs included.
    "vip": [
        "XAUUSD",

        # Major pairs
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",

        # High-liquidity cross pairs
        "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY",
        "EURAUD", "EURCAD", "GBPAUD", "CADJPY", "CHFJPY",
    ],
}

# Per-symbol configuration: display, pip size, decimals, ATR floor, feeds
SYMBOL_CFG = {
    # ----------------------------- GOLD -----------------------------
    "XAUUSD": {
        "label": "GOLD",
        "emoji": "🟡",
        "pip": 0.1,
        "dec": 2,
        "atr_floor": 0.5,
        "td": "XAU/USD",
        "yahoo": "GC=F",
        "gold_api": "XAU",
    },

    # ----------------------------- MAJOR PAIRS -----------------------------
    "EURUSD": {
        "label": "EUR/USD",
        "emoji": "🔵",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0008,
        "td": "EUR/USD",
        "yahoo": "EURUSD=X",
    },
    "GBPUSD": {
        "label": "GBP/USD",
        "emoji": "🔵",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0008,
        "td": "GBP/USD",
        "yahoo": "GBPUSD=X",
    },
    "USDJPY": {
        "label": "USD/JPY",
        "emoji": "🔴",
        "pip": 0.01,
        "dec": 3,
        "atr_floor": 0.05,
        "td": "USD/JPY",
        "yahoo": "USDJPY=X",
    },
    "USDCHF": {
        "label": "USD/CHF",
        "emoji": "🔵",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0008,
        "td": "USD/CHF",
        "yahoo": "USDCHF=X",
    },
    "USDCAD": {
        "label": "USD/CAD",
        "emoji": "🔵",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0008,
        "td": "USD/CAD",
        "yahoo": "USDCAD=X",
    },
    "AUDUSD": {
        "label": "AUD/USD",
        "emoji": "🔵",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0008,
        "td": "AUD/USD",
        "yahoo": "AUDUSD=X",
    },
    "NZDUSD": {
        "label": "NZD/USD",
        "emoji": "🔵",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0008,
        "td": "NZD/USD",
        "yahoo": "NZDUSD=X",
    },

    # ----------------------------- LIQUID CROSS PAIRS -----------------------------
    "EURGBP": {
        "label": "EUR/GBP",
        "emoji": "🟣",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0007,
        "td": "EUR/GBP",
        "yahoo": "EURGBP=X",
    },
    "EURJPY": {
        "label": "EUR/JPY",
        "emoji": "🔴",
        "pip": 0.01,
        "dec": 3,
        "atr_floor": 0.05,
        "td": "EUR/JPY",
        "yahoo": "EURJPY=X",
    },
    "GBPJPY": {
        "label": "GBP/JPY",
        "emoji": "🔴",
        "pip": 0.01,
        "dec": 3,
        "atr_floor": 0.08,
        "td": "GBP/JPY",
        "yahoo": "GBPJPY=X",
    },
    "AUDJPY": {
        "label": "AUD/JPY",
        "emoji": "🔴",
        "pip": 0.01,
        "dec": 3,
        "atr_floor": 0.05,
        "td": "AUD/JPY",
        "yahoo": "AUDJPY=X",
    },
    "NZDJPY": {
        "label": "NZD/JPY",
        "emoji": "🔴",
        "pip": 0.01,
        "dec": 3,
        "atr_floor": 0.05,
        "td": "NZD/JPY",
        "yahoo": "NZDJPY=X",
    },
    "EURAUD": {
        "label": "EUR/AUD",
        "emoji": "🟣",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0010,
        "td": "EUR/AUD",
        "yahoo": "EURAUD=X",
    },
    "EURCAD": {
        "label": "EUR/CAD",
        "emoji": "🟣",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0009,
        "td": "EUR/CAD",
        "yahoo": "EURCAD=X",
    },
    "GBPAUD": {
        "label": "GBP/AUD",
        "emoji": "🟣",
        "pip": 0.0001,
        "dec": 4,
        "atr_floor": 0.0012,
        "td": "GBP/AUD",
        "yahoo": "GBPAUD=X",
    },
    "CADJPY": {
        "label": "CAD/JPY",
        "emoji": "🔴",
        "pip": 0.01,
        "dec": 3,
        "atr_floor": 0.05,
        "td": "CAD/JPY",
        "yahoo": "CADJPY=X",
    },
    "CHFJPY": {
        "label": "CHF/JPY",
        "emoji": "🔴",
        "pip": 0.01,
        "dec": 3,
        "atr_floor": 0.05,
        "td": "CHF/JPY",
        "yahoo": "CHFJPY=X",
    },
}

HISTORY_MAX = 800            # max price points kept per symbol
WARMUP      = 160            # smarter mode needs more history before signalling
LOOKBACK    = 60             # high/low detection window
EXCLUDE     = 6              # last N points excluded from the H/L (breakout room)
ATR_POINTS  = 20             # points used for the volatility estimate
SL_ATR      = 2.1            # controlled risk; old version was wider and late
TP1_ATR     = 3.0            # take-profit-1 distance in ATRs
TP2_ATR     = 4.8            # take-profit-2 distance in ATRs
COOLDOWN_S  = 120 * 60       # minimum time between new signals per symbol

# -------------------------- VIP safety controls -----------------------------
# This version is built for subscriber protection: fewer signals, better filters.
# It scans all pairs, ranks valid setups, then posts only the best one.
MIN_CONF_VIP        = int(os.environ.get("VIP_MIN_CONF", "89"))  # code posts only ABOVE this -> 90 only by default
LOSS_COOLDOWN_S     = int(os.environ.get("LOSS_COOLDOWN_HOURS", "8")) * 3600
GLOBAL_LOSS_PAUSE_S = int(os.environ.get("GLOBAL_LOSS_PAUSE_HOURS", "4")) * 3600
MAX_VIP_PER_DAY     = int(os.environ.get("MAX_VIP_SIGNALS_PER_DAY", "4"))
MAX_SYMBOL_PER_DAY  = int(os.environ.get("MAX_SYMBOL_SIGNALS_PER_DAY", "1"))
MAX_POSTS_PER_RUN   = int(os.environ.get("MAX_POSTS_PER_RUN", "1"))
MAX_SPREAD_ATR      = float(os.environ.get("MAX_BREAKOUT_ATR", "2.4"))
MIN_BREAK_ATR       = float(os.environ.get("MIN_BREAKOUT_ATR", "0.45"))
MIN_EFFICIENCY      = float(os.environ.get("MIN_TREND_EFFICIENCY", "0.32"))

# Trade only during better liquidity windows in London time.
# Format: "07:00-11:30,13:00-17:00". Set empty to disable.
SESSION_WINDOWS_LOCAL = os.environ.get("SESSION_WINDOWS_LOCAL", "07:00-11:30,13:00-17:00")

# Optional manual high-impact news pause windows in UTC.
# Format example: "2026-08-26 12:00-13:30;2026-08-27 18:30-19:30"
NEWS_PAUSE_UTC = os.environ.get("NEWS_PAUSE_UTC", "")

TZ       = dt.timezone.utc
LOCAL_TZ = ZoneInfo("Europe/London")


# ------------------------------------------------------------------ helpers ---

def log(msg):
    print("[%s] %s" % (dt.datetime.now(TZ).strftime("%H:%M:%S"), msg), flush=True)


def http_json(url, timeout=25):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; gold-hl-bot/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def send_telegram(chat_id, text):
    """Send a message. DRY-RUN (no token/chat) prints to stdout instead."""
    if not TOKEN or not chat_id:
        log("DRY-RUN — would post to %s:\n%s\n" % (chat_id or "(no chat id)", text))
        return True
    api = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        req = urllib.request.Request(api, data=data)
        with urllib.request.urlopen(req, timeout=25) as resp:
            ok = json.loads(resp.read().decode()).get("ok")
        if ok:
            log("Posted message to %s" % chat_id)
            return True
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        log("Telegram error %s: %s" % (e.code, body))
        raise RuntimeError(body)
    except Exception as e:
        log("Telegram send failed: %s" % e)
        raise


def fmt(sym, price):
    return "%.*f" % (SYMBOL_CFG[sym]["dec"], price)


def pips_for(sym, side, entry, exit_):
    d = (exit_ - entry) if side == "BUY" else (entry - exit_)
    return d / SYMBOL_CFG[sym]["pip"]

# ------------------------------------------------------------------- state ----

def new_symbol_state():
    # history keeps backward-compatible [timestamp, close] points.
    # candles keeps genuine OHLC [timestamp, open, high, low, close] points.
    return {"history": [], "candles": [], "open": None, "closed": [], "last_signal_ts": 0}


def new_state():
    return {"symbols": {}}


def load_state(path=None):
    """Load state; transparently migrate old single-symbol (flat) format."""
    p = path or STATE_FILE
    try:
        with open(p) as f:
            raw = json.load(f)
    except Exception:
        return new_state()
    if "symbols" in raw:
        st = raw
        for sym, ss in st["symbols"].items():
            base = new_symbol_state()
            for k, v in base.items():
                ss.setdefault(k, v)
        return st
    # ---- old flat format (gold-only era) -> nest under XAUUSD
    migrated = new_state()
    hist = raw.get("history", [])
    migrated["symbols"]["XAUUSD"] = {
        "history": hist,
        "candles": [[int(ts), float(p), float(p), float(p), float(p)] for ts, p in hist],
        "open": raw.get("open"),
        "closed": raw.get("closed", []),
        "last_signal_ts": raw.get("last_signal_ts", 0),
    }
    log("Migrated legacy gold-only state into multi-symbol format.")
    return migrated


def save_state(st, path=None):
    for sym, ss in st["symbols"].items():
        ss.setdefault("candles", [])
        ss["history"] = ss["history"][-HISTORY_MAX:]
        ss["candles"] = ss["candles"][-HISTORY_MAX:]
        ss["closed"]  = ss["closed"][-300:]
    p = path or STATE_FILE
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)
    total = sum(len(ss["history"]) for ss in st["symbols"].values())
    log("State saved (%d history points across %d symbols)"
        % (total, len(st["symbols"])))

# ------------------------------------------------------------------ prices ----

def normalise_candle(c):
    """Return [ts, open, high, low, close] with sane OHLC ordering."""
    ts, o, h, l, close = c
    vals = [float(o), float(h), float(l), float(close)]
    o, h, l, close = vals
    h = max(h, o, close)
    l = min(l, o, close)
    return [int(round(ts)), o, h, l, close]


def merge_candles(sym_state, candles):
    if not candles:
        return
    sym_state.setdefault("candles", [])
    by_ts = {int(round(c[0])): normalise_candle(c) for c in sym_state.get("candles", []) if len(c) >= 5}
    for c in candles:
        if len(c) >= 5 and c[4] and c[4] > 0:
            nc = normalise_candle(c)
            by_ts[nc[0]] = nc
    merged = [by_ts[k] for k in sorted(by_ts)][-HISTORY_MAX:]
    sym_state["candles"] = merged
    # Maintain old close-only history for compatibility with older state/recap logic.
    sym_state["history"] = [[c[0], c[4]] for c in merged]


def merge_history(sym_state, points):
    """Backward-compatible close-only merge. Also mirrors points into candles."""
    if not points:
        return
    candles = [[ts, price, price, price, price] for ts, price in points if price and price > 0]
    merge_candles(sym_state, candles)


def fetch_candles(sym):
    """Return [[unix_ts, open, high, low, close], ...] ascending. May be empty."""
    cfg = SYMBOL_CFG[sym]
    if TD_KEY:
        try:
            d = http_json(
                "https://api.twelvedata.com/time_series"
                "?symbol=%s&interval=5min&outputsize=200&apikey=%s"
                % (urllib.parse.quote(cfg["td"]), TD_KEY)
            )
            vals = d.get("values") or []
            candles = []
            for v in vals:
                t = dt.datetime.fromisoformat(v["datetime"][:19]).replace(tzinfo=TZ)
                o = float(v.get("open") or v.get("close"))
                h = float(v.get("high") or v.get("close"))
                l = float(v.get("low") or v.get("close"))
                c = float(v["close"])
                candles.append([int(t.timestamp()), o, h, l, c])
            candles.sort(key=lambda x: x[0])
            if candles:
                log("  %s: Twelve Data OHLC (%d candles)" % (cfg["label"], len(candles)))
                return candles
        except Exception as e:
            log("  %s: Twelve Data failed: %s" % (cfg["label"], e))
    try:
        d = http_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                      "%s?interval=5m&range=5d" % urllib.parse.quote(cfg["yahoo"]))
        r = d["chart"]["result"][0]
        ts = r.get("timestamp") or []
        q = (r["indicators"]["quote"][0] or {})
        op, hi, lo, cl = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
        candles = []
        for t, o, h, l, c in zip(ts, op, hi, lo, cl):
            if c is None:
                continue
            o = c if o is None else o
            h = c if h is None else h
            l = c if l is None else l
            candles.append(normalise_candle([int(t), float(o), float(h), float(l), float(c)]))
        if candles:
            log("  %s: Yahoo OHLC (%d candles)" % (cfg["label"], len(candles)))
            return candles
    except Exception as e:
        log("  %s: Yahoo failed: %s" % (cfg["label"], e))
    if cfg.get("gold_api"):
        try:
            d = http_json("https://api.gold-api.com/price/%s" % cfg["gold_api"])
            price = float(d["price"])
            candles = [[int(time.time()), price, price, price, price]]
            log("  %s: gold-api.com spot" % cfg["label"])
            return candles
        except Exception as e:
            log("  %s: gold-api failed: %s" % (cfg["label"], e))
    return []


def fetch_prices(sym):
    """Backward-compatible close-only wrapper."""
    return [[c[0], c[4]] for c in fetch_candles(sym)]

# ---------------------------------------------------------------- strategy ----

def closes_from_candles(candles):
    return [float(c[4]) for c in candles if len(c) >= 5]


def highs_from_candles(candles):
    return [float(c[2]) for c in candles if len(c) >= 5]


def lows_from_candles(candles):
    return [float(c[3]) for c in candles if len(c) >= 5]


def ema(values, period):
    if not values:
        return None
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def true_atr(sym, candles, period=ATR_POINTS):
    """True ATR from OHLC: max(high-low, abs(high-prev_close), abs(low-prev_close))."""
    if len(candles) < 2:
        return SYMBOL_CFG[sym]["atr_floor"]
    trs = []
    for i in range(1, len(candles)):
        _, _, h, l, c = candles[i]
        prev_c = candles[i - 1][4]
        trs.append(max(float(h) - float(l), abs(float(h) - prev_c), abs(float(l) - prev_c)))
    if not trs:
        return SYMBOL_CFG[sym]["atr_floor"]
    a = sum(trs[-period:]) / min(len(trs), period)
    return max(a, SYMBOL_CFG[sym]["atr_floor"])


def atr_estimate(sym, closes_or_candles):
    """Compatibility wrapper. Uses true ATR when OHLC candles are supplied."""
    if closes_or_candles and isinstance(closes_or_candles[0], (list, tuple)) and len(closes_or_candles[0]) >= 5:
        return true_atr(sym, closes_or_candles)
    closes = closes_or_candles
    diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    if not diffs:
        return SYMBOL_CFG[sym]["atr_floor"]
    a = sum(diffs[-ATR_POINTS:]) / min(len(diffs), ATR_POINTS)
    return max(a, SYMBOL_CFG[sym]["atr_floor"])


def efficiency_ratio(closes, n=30):
    """Trend quality: 0 = chop, 1 = clean one-way movement."""
    if len(closes) <= n:
        return 0.0
    net = abs(closes[-1] - closes[-1 - n])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes)))
    return net / max(path, 1e-12)


def market_regime(sym, candles):
    closes = closes_from_candles(candles)
    if len(candles) < 80:
        return {"name": "WARMUP", "tradeable": False, "reason": "not enough candles"}
    a = true_atr(sym, candles)
    er = efficiency_ratio(closes, 30)
    recent_atr = true_atr(sym, candles[-30:]) if len(candles) >= 35 else a
    long_atr = true_atr(sym, candles[-160:]) if len(candles) >= 170 else a
    atr_ratio = recent_atr / max(long_atr, 1e-9)
    fast, slow = ema(closes[-80:], 12), ema(closes[-120:], 48)
    trend_gap_atr = abs(fast - slow) / max(a, 1e-9)
    if atr_ratio > 2.2:
        return {"name": "VOLATILE_SPIKE", "tradeable": False, "reason": "volatility spike", "er": er, "atr_ratio": atr_ratio}
    if atr_ratio < 0.35:
        return {"name": "QUIET", "tradeable": False, "reason": "low volatility", "er": er, "atr_ratio": atr_ratio}
    if er < MIN_EFFICIENCY or trend_gap_atr < 0.25:
        return {"name": "CHOPPY", "tradeable": False, "reason": "choppy/ranging", "er": er, "atr_ratio": atr_ratio}
    return {"name": "TRENDING", "tradeable": True, "reason": "trend regime", "er": er, "atr_ratio": atr_ratio}


def recent_momentum_score(side, closes, a):
    if len(closes) < 10:
        return 0
    m3 = closes[-1] - closes[-4]
    m6 = closes[-1] - closes[-7]
    directional = (m3 + 0.7 * m6) if side == "BUY" else (-m3 - 0.7 * m6)
    return max(0, min(100, int(round((directional / max(a, 1e-9)) * 35))))


def yahoo_candles(sym, interval="15m", range_="10d"):
    cfg = SYMBOL_CFG[sym]
    d = http_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                  "%s?interval=%s&range=%s" % (
                      urllib.parse.quote(cfg["yahoo"]), interval, range_))
    r = d["chart"]["result"][0]
    ts = r.get("timestamp") or []
    q = (r["indicators"]["quote"][0] or {})
    op, hi, lo, cl = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
    out = []
    for t, o, h, l, c in zip(ts, op, hi, lo, cl):
        if c is None:
            continue
        o = c if o is None else o
        h = c if h is None else h
        l = c if l is None else l
        out.append(normalise_candle([int(t), float(o), float(h), float(l), float(c)]))
    return out


def yahoo_closes(sym, interval="15m", range_="10d"):
    return closes_from_candles(yahoo_candles(sym, interval, range_))


def higher_timeframe_score(sym, side):
    """15m + 1h confirmation. Returns (score 0-100, reason)."""
    try:
        c15 = yahoo_closes(sym, "15m", "10d")
        c1h = yahoo_closes(sym, "60m", "1mo")
        if len(c15) < 120 or len(c1h) < 120:
            return 0, "not enough MTF data"
        f15, s15 = ema(c15[-120:], 20), ema(c15[-120:], 80)
        f1h, s1h = ema(c1h[-120:], 20), ema(c1h[-120:], 80)
        score = 0
        if side == "BUY":
            score += 45 if f15 > s15 else 0
            score += 45 if f1h > s1h else 0
            score += 10 if c15[-1] > f15 else 0
        else:
            score += 45 if f15 < s15 else 0
            score += 45 if f1h < s1h else 0
            score += 10 if c15[-1] < f15 else 0
        return score, "15m/1h aligned" if score >= 90 else "15m/1h weak"
    except Exception as e:
        log("  %s: MTF check failed: %s" % (SYMBOL_CFG[sym]["label"], e))
        return 0, "MTF data failed"


def higher_timeframe_ok(sym, side):
    score, reason = higher_timeframe_score(sym, side)
    return score >= 90, reason


def highlow_score(sym, side, closes, candles, hi, lo, a, margin_atr, regime):
    fast, slow = ema(closes[-80:], 12), ema(closes[-120:], 48)
    mid_fast, mid_slow = ema(closes[-160:], 20), ema(closes[-160:], 80)
    trend_gap_atr = abs(fast - slow) / max(a, 1e-9)
    structure_er = efficiency_ratio(closes, 30)
    trend = max(0, min(100, int(round(trend_gap_atr * 55))))
    structure = max(0, min(100, int(round(structure_er * 140))))
    momentum = recent_momentum_score(side, closes, a)
    breakout = max(0, min(100, int(round(100 - abs(margin_atr - 1.15) * 35))))
    mtf, mtf_reason = higher_timeframe_score(sym, side)
    regime_bonus = 5 if regime.get("name") == "TRENDING" else -20
    score = int(round(0.24 * trend + 0.18 * structure + 0.18 * momentum + 0.20 * breakout + 0.20 * mtf + regime_bonus))
    score = max(0, min(100, score))
    return score, {
        "trend": trend, "structure": structure, "momentum": momentum,
        "breakout": breakout, "mtf": mtf, "regime": regime.get("name"),
        "mtf_reason": mtf_reason, "trend_gap": round(trend_gap_atr, 3),
        "efficiency": round(structure_er, 3), "margin_atr": round(margin_atr, 3),
    }


def analyze(sym, candles):
    """Return signal dict or None using genuine OHLC + no-trade rules.

    HighLow Score is a 0-100 setup-quality score. It is NOT win probability.
    """
    if not candles or len(candles[0]) < 5:
        candles = [[ts, p, p, p, p] for ts, p in candles]
    if len(candles) < WARMUP:
        return None
    closes = closes_from_candles(candles)
    highs = highs_from_candles(candles)
    lows = lows_from_candles(candles)
    regime = market_regime(sym, candles)
    if not regime.get("tradeable"):
        log("  %s: NO-TRADE — %s" % (SYMBOL_CFG[sym]["label"], regime.get("reason")))
        return None

    window_highs = highs[-(LOOKBACK + EXCLUDE):-EXCLUDE]
    window_lows = lows[-(LOOKBACK + EXCLUDE):-EXCLUDE]
    hi, lo = max(window_highs), min(window_lows)
    last, prev = closes[-1], closes[-2]
    fast, slow = ema(closes[-80:], 12), ema(closes[-120:], 48)
    mid_fast, mid_slow = ema(closes[-160:], 20), ema(closes[-160:], 80)
    a = true_atr(sym, candles)

    # Reject one-candle news-like spikes.
    last_range_atr = (candles[-1][2] - candles[-1][3]) / max(a, 1e-9)
    if last_range_atr > 2.0:
        log("  %s: NO-TRADE — spike candle" % SYMBOL_CFG[sym]["label"])
        return None

    volatile = sym in ("XAUUSD", "GBPJPY", "GBPAUD")
    trend_gap_atr = abs(fast - slow) / max(a, 1e-9)
    trend_min = 0.35 + (0.18 if sym == "XAUUSD" else 0.08 if volatile else 0.0)
    if trend_gap_atr < trend_min:
        return None

    def build(side, margin, ref_level, kind):
        margin_atr = margin / max(a, 1e-9)
        if margin_atr < MIN_BREAK_ATR or margin_atr > MAX_SPREAD_ATR:
            return None
        momentum = recent_momentum_score(side, closes, a)
        if momentum < 45:
            return None
        score, components = highlow_score(sym, side, closes, candles, hi, lo, a, margin_atr, regime)
        if components["mtf"] < 90:
            log("  %s: skipped — %s" % (SYMBOL_CFG[sym]["label"], components["mtf_reason"]))
            return None
        if sym == "XAUUSD":
            score = max(0, score - 3)
        return {"side": side, "level": last, "hi": hi, "lo": lo, "atr": a,
                "conf": score, "highlow_score": score, "score_components": components,
                "regime": regime.get("name"), "kind": kind % fmt(sym, ref_level)}

    # Break-and-hold confirmation uses two closes beyond structure level.
    if last > hi and prev > hi and fast > slow and mid_fast > mid_slow:
        return build("BUY", last - hi, hi, "clean OHLC breakout above %s")
    if last < lo and prev < lo and fast < slow and mid_fast < mid_slow:
        return build("SELL", lo - last, lo, "clean OHLC breakdown below %s")
    return None


def confidence(sym, sig):
    """HighLow Score, not a win probability."""
    return int(sig.get("highlow_score", sig.get("conf", 0)))


def make_trade(sym, sig, now):
    """Create a complete trade object matching the displayed SL/TP and management logic."""
    entry = float(sig["level"])
    a = float(sig["atr"])
    if sig["side"] == "BUY":
        sl, tp1, tp2 = entry - SL_ATR * a, entry + TP1_ATR * a, entry + TP2_ATR * a
    else:
        sl, tp1, tp2 = entry + SL_ATR * a, entry - TP1_ATR * a, entry - TP2_ATR * a
    score = confidence(sym, sig)
    return {"symbol": sym, "side": sig["side"], "kind": sig["kind"],
            "entry": round(entry, 6), "sl": round(sl, 6), "initial_sl": round(sl, 6),
            "tp1": round(tp1, 6), "tp2": round(tp2, 6), "atr": round(a, 6),
            "conf": score, "highlow_score": score,
            "score_components": sig.get("score_components", {}),
            "regime": sig.get("regime", "UNKNOWN"),
            "opened_ts": now, "tp1_hit": False, "tp2_hit": False,
            "break_even": False, "partial_pct": 50, "remaining_pct": 100,
            "realized_pips": 0.0,
            "management": "TP1 secures 50% and moves SL to break-even; TP2 closes remainder."}

# ---------------------------------------------------------------- messages ----

def ts_str(ts):
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%d %b %Y, %H:%M UTC")


def signal_message(sym, tr, vip):
    cfg = SYMBOL_CFG[sym]
    head = "%s %s SIGNAL — %s %s" % (
        cfg["emoji"], cfg["label"], tr["side"],
        "\u2B06\uFE0F" if tr["side"] == "BUY" else "\u2B07\uFE0F")
    if vip:
        comps = tr.get("score_components", {}) or {}
        comp_line = "Trend %s | Structure %s | Momentum %s | Breakout %s | MTF %s" % (
            comps.get("trend", "-"), comps.get("structure", "-"),
            comps.get("momentum", "-"), comps.get("breakout", "-"), comps.get("mtf", "-"))
        return "\n".join([
            head,
            "\U0001F48E VIP — %s" % tr["kind"],
            "",
            "Entry: %s" % fmt(sym, tr["entry"]),
            "Stop loss: %s  (%d pips)" % (fmt(sym, tr["sl"]), abs(pips_for(sym, tr["side"], tr["entry"], tr["sl"]))),
            "TP 1: %s  (%d pips)" % (fmt(sym, tr["tp1"]), abs(pips_for(sym, tr["side"], tr["entry"], tr["tp1"]))),
            "TP 2: %s  (%d pips)" % (fmt(sym, tr["tp2"]), abs(pips_for(sym, tr["side"], tr["entry"], tr["tp2"]))),
            "",
            "HighLow Score: %d/100 (setup quality, not win probability)" % tr["highlow_score"],
            "Components: " + comp_line,
            "Market regime: %s" % tr.get("regime", "UNKNOWN"),
            "Management: TP1 partial + SL to break-even; TP2 final target.",
            ts_str(tr["opened_ts"]),
        ])
    lines = [
        head,
        "Entry: %s  (%s)" % (fmt(sym, tr["entry"]), tr["kind"]),
        "HighLow Score: %d/100" % tr.get("highlow_score", tr.get("conf", 0)),
        "",
        "Full SL + TP1/TP2 levels, live updates & daily recap \u2192 VIP",
    ]
    if FOOTER:
        lines.append("\U0001F449 Upgrade: %s" % FOOTER)
    lines.append(ts_str(tr["opened_ts"]))
    return "\n".join(lines)


def tp1_message(sym, tr):
    cfg = SYMBOL_CFG[sym]
    p = pips_for(sym, tr["side"], tr["entry"], tr["tp1"])
    return "\n".join([
        "\U0001F7E9 %s TP1 HIT — %s" % (cfg["label"], tr["side"]),
        "TP1: %s (%+.0f pips)" % (fmt(sym, tr["tp1"]), p),
        "50%% secured. Stop loss moved to break-even: %s" % fmt(sym, tr["entry"]),
        "TP2 remains: %s" % fmt(sym, tr["tp2"]),
        ts_str(int(time.time())),
    ])


def close_message(sym, tr, exit_price, reason, win):
    cfg = SYMBOL_CFG[sym]
    p = tr.get("result_pips")
    if p is None:
        p = pips_for(sym, tr["side"], tr["entry"], exit_price)
    emoji = "\u2705" if win else "\u274C"
    return "\n".join([
        "%s %s CLOSED — %s from %s" % (emoji, cfg["label"], tr["side"], fmt(sym, tr["entry"])),
        "Exit: %s (%s)" % (fmt(sym, exit_price), reason),
        "Result: %+.1f pips %s" % (p, "\U0001F7E9" if p >= 0 else "\U0001F5E5"),
        "HighLow Score was: %s/100" % tr.get("highlow_score", tr.get("conf", "-")),
        ts_str(int(time.time())),
    ])


def recap_message(stats, vip):
    lines = ["\U0001F4CA DAILY RECAP — %s" %
             dt.datetime.now(TZ).strftime("%d %b %Y"), ""]
    for label, s in (("VIP", stats.get("vip")), ("Free", stats.get("free"))):
        if s and s["signals"]:
            per = ", ".join("%s %d" % (SYMBOL_CFG.get(k, {"label": str(k)})["label"], v)
                            for k, v in sorted(s["per_symbol"].items()) if v)
            lines.append("%s: %d signals (%s) \u00B7 %dW/%dL \u00B7 %d%% \u00B7 %+.0f pips" % (
                label, s["signals"], per or "-", s["wins"], s["losses"],
                s["winrate"], s["pips"]))
        else:
            lines.append("%s: no closed signals" % label)
    best = stats.get("best")
    if best:
        lines += ["", "Best trade: %s %s %+.0f pips" % (
            SYMBOL_CFG.get(best.get("symbol"), {"label": str(best.get("symbol"))})["label"], best["side"], best["pips"])]
        if not vip and FOOTER:
            lines.append("Get every signal: %s" % FOOTER)
    return "\n".join(lines)


def welcome_message(role):
    if role == "vip":
        pairs = ", ".join(SYMBOL_CFG[s]["label"] for s in SYMBOLS_BY_ROLE["vip"])
        return "\n".join([
            "\u2705 GOLD HL BOT — VIP channel is LIVE",
            "",
            "VIP watches: %s" % pairs,
            "Sniper mode: only setups above %d%% confidence get posted." % MIN_CONF_VIP,
            "Full signals with SL + TP1/TP2 land here every 5 minutes.",
            "Daily recap arrives at 21:05 UTC.",
            ts_str(int(time.time())),
        ])
    lines = [
        "\u2705 GOLD HL BOT — free channel is LIVE",
        "",
        "GOLD signals post here every 15 minutes.",
        "GOLD + FOREX with full SL/TP every 5 min \u2192 VIP",
    ]
    if FOOTER:
        lines.append("\U0001F449 " + FOOTER)
    lines.append(ts_str(int(time.time())))
    return "\n".join(lines)

# ------------------------------------------------------------------- roles ----

def parse_hhmm(s):
    h, m = s.split(":", 1)
    return int(h) * 60 + int(m)


def in_session(now=None):
    """London-session filter. Set SESSION_WINDOWS_LOCAL empty to disable."""
    if not SESSION_WINDOWS_LOCAL.strip():
        return True
    now = now or dt.datetime.now(LOCAL_TZ)
    mins = now.hour * 60 + now.minute
    for part in SESSION_WINDOWS_LOCAL.split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        a, b = part.split("-", 1)
        start, end = parse_hhmm(a.strip()), parse_hhmm(b.strip())
        if start <= mins <= end:
            return True
    return False


def in_manual_news_pause(now=None):
    """Manual news pause. Use NEWS_PAUSE_UTC env when high-impact news is expected."""
    if not NEWS_PAUSE_UTC.strip():
        return False
    now = now or dt.datetime.now(TZ)
    for block in NEWS_PAUSE_UTC.split(";"):
        block = block.strip()
        if not block:
            continue
        try:
            date_part, times = block.split(" ", 1)
            a, b = times.split("-", 1)
            start = dt.datetime.fromisoformat(date_part + "T" + a.strip() + ":00").replace(tzinfo=TZ)
            end = dt.datetime.fromisoformat(date_part + "T" + b.strip() + ":00").replace(tzinfo=TZ)
            if start <= now <= end:
                return True
        except Exception:
            continue
    return False


def closed_trades_from_state(st):
    trades = []
    for sym, ss in st.get("symbols", {}).items():
        for t in ss.get("closed", []):
            tt = dict(t)
            if not tt.get("symbol"):
                tt["symbol"] = sym
            trades.append(tt)
    trades.sort(key=lambda x: x.get("closed_ts", 0), reverse=True)
    return trades


def count_trades_today(trades, sym=None):
    start = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return sum(1 for t in trades
               if t.get("opened_ts", t.get("closed_ts", 0)) >= start
               and (sym is None or t.get("symbol") == sym))


def symbol_loss_pause(sym_state, now):
    closed = sorted(sym_state.get("closed", []), key=lambda x: x.get("closed_ts", 0), reverse=True)
    if not closed:
        return False
    last = closed[0]
    return (not last.get("win", False)) and now - last.get("closed_ts", 0) < LOSS_COOLDOWN_S


def global_loss_pause(st, now):
    """After a losing streak, stop opening new VIP trades temporarily."""
    trades = [t for t in closed_trades_from_state(st) if t.get("closed_ts", 0) > now - 24 * 3600]
    if len(trades) < 3:
        return False
    last3 = trades[:3]
    if all(not t.get("win", False) for t in last3):
        return now - last3[0].get("closed_ts", 0) < GLOBAL_LOSS_PAUSE_S
    return False


def opened_trades_from_state(st):
    trades = []
    for sym, ss in st.get("symbols", {}).items():
        if ss.get("open"):
            t = dict(ss["open"])
            t.setdefault("symbol", sym)
            trades.append(t)
        for t in ss.get("closed", []):
            tt = dict(t)
            if not tt.get("symbol"):
                tt["symbol"] = sym
            trades.append(tt)
    return trades


def count_opened_today(st, sym=None):
    start = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return sum(1 for t in opened_trades_from_state(st)
               if t.get("opened_ts", 0) >= start and (sym is None or t.get("symbol") == sym))


def performance_stats(st):
    trades = closed_trades_from_state(st)
    if not trades:
        return {"closed": 0, "wins": 0, "losses": 0, "winrate": 0, "pips": 0.0, "last10": "-"}
    wins = [t for t in trades if t.get("win")]
    last10 = trades[:10]
    last10_wins = sum(1 for t in last10 if t.get("win"))
    return {
        "closed": len(trades), "wins": len(wins), "losses": len(trades) - len(wins),
        "winrate": int(100 * len(wins) / len(trades)),
        "pips": round(sum(float(t.get("pips", 0)) for t in trades), 1),
        "last10": "%dW/%dL" % (last10_wins, len(last10) - last10_wins),
        "updated_ts": int(time.time()),
    }


def candle_hit_levels(side, candle, sl, tp1=None, tp2=None):
    """Conservative OHLC level-hit detection on the latest closed candle."""
    _, o, h, l, c = candle
    if side == "BUY":
        return {"sl": l <= sl, "tp1": tp1 is not None and h >= tp1, "tp2": tp2 is not None and h >= tp2}
    return {"sl": h >= sl, "tp1": tp1 is not None and l <= tp1, "tp2": tp2 is not None and l <= tp2}


def blended_result_pips(sym, tr, exit_price, reason):
    side, entry = tr["side"], tr["entry"]
    if tr.get("tp1_hit"):
        p1 = pips_for(sym, side, entry, tr["tp1"]) * (tr.get("partial_pct", 50) / 100.0)
        p2 = pips_for(sym, side, entry, exit_price) * (tr.get("remaining_pct", 50) / 100.0)
        return round(p1 + p2, 1)
    return round(pips_for(sym, side, entry, exit_price), 1)


def manage_open_trade(sym, sym_state, chat):
    """Manage TP1, break-even and TP2 using OHLC, not close-only prices."""
    candles = sym_state.get("candles") or []
    if not candles and sym_state.get("history"):
        candles = [[ts, p, p, p, p] for ts, p in sym_state["history"]]
    if not candles:
        return False
    candle = candles[-1]
    price = candle[4]
    now = int(time.time())
    tr = sym_state.get("open")
    if not tr:
        return False

    tr.setdefault("tp1_hit", False)
    tr.setdefault("tp2_hit", False)
    tr.setdefault("break_even", False)
    tr.setdefault("partial_pct", 50)
    tr.setdefault("remaining_pct", 100 if not tr.get("tp1_hit") else 50)
    tr.setdefault("initial_sl", tr.get("sl"))

    hits = candle_hit_levels(tr["side"], candle, tr["sl"], tr["tp1"], tr["tp2"])

    # Conservative ordering before TP1: if SL and TP1 appear in the same candle,
    # count SL first. This avoids overstating results with 5m OHLC ambiguity.
    if not tr["tp1_hit"] and hits["sl"]:
        exit_price = tr["sl"]
        result = blended_result_pips(sym, tr, exit_price, "SL")
        tr["result_pips"] = result
        send_telegram(chat, close_message(sym, tr, exit_price, "SL", result > 0))
        sym_state["closed"].append({**tr, "symbol": sym, "exit": exit_price, "reason": "SL",
                                    "win": result > 0, "closed_ts": now, "pips": result})
        sym_state["open"] = None
        return True

    if not tr["tp1_hit"] and hits["tp1"]:
        tr["tp1_hit"] = True
        tr["break_even"] = True
        tr["remaining_pct"] = 100 - tr.get("partial_pct", 50)
        tr["realized_pips"] = round(pips_for(sym, tr["side"], tr["entry"], tr["tp1"]) * (tr.get("partial_pct", 50) / 100.0), 1)
        tr["sl"] = tr["entry"]  # move remaining position to break-even
        send_telegram(chat, tp1_message(sym, tr))
        log("  %s: TP1 hit; SL moved to break-even" % SYMBOL_CFG[sym]["label"])
        # Same candle may also reach TP2. Allow TP2 after TP1.
        hits = candle_hit_levels(tr["side"], candle, tr["sl"], tr["tp1"], tr["tp2"])

    if tr.get("tp1_hit") and hits["tp2"]:
        exit_price = tr["tp2"]
        result = blended_result_pips(sym, tr, exit_price, "TP2")
        tr["tp2_hit"] = True
        tr["result_pips"] = result
        send_telegram(chat, close_message(sym, tr, exit_price, "TP2", True))
        sym_state["closed"].append({**tr, "symbol": sym, "exit": exit_price, "reason": "TP2",
                                    "win": True, "closed_ts": now, "pips": result})
        sym_state["open"] = None
        return True

    if tr.get("tp1_hit") and hits["sl"]:
        exit_price = tr["sl"]
        result = blended_result_pips(sym, tr, exit_price, "BE")
        tr["result_pips"] = result
        reason = "BE after TP1" if abs(exit_price - tr["entry"]) < 1e-12 else "SL after TP1"
        send_telegram(chat, close_message(sym, tr, exit_price, reason, result > 0))
        sym_state["closed"].append({**tr, "symbol": sym, "exit": exit_price, "reason": reason,
                                    "win": result > 0, "closed_ts": now, "pips": result})
        sym_state["open"] = None
        return True

    log("  %s: open %s from %s still running (now %s)"
        % (SYMBOL_CFG[sym]["label"], tr["side"], fmt(sym, tr["entry"]), fmt(sym, price)))
    return False


def scan_symbol_for_candidate(sym, sym_state, chat, vip, allow_new=True, st=None):
    """Update one symbol, manage open trade, and return a ranked candidate or None."""
    cfg = SYMBOL_CFG[sym]
    candles = fetch_candles(sym)
    merge_candles(sym_state, candles)
    candles = sym_state.get("candles") or [[ts, p, p, p, p] for ts, p in sym_state.get("history", [])]

    if len(candles) < 2:
        log("  %s: warming up (%d candles)" % (cfg["label"], len(candles)))
        return None

    price = candles[-1][4]
    now = int(time.time())
    log("  %s: %s | %d candles" % (cfg["label"], fmt(sym, price), len(candles)))

    manage_open_trade(sym, sym_state, chat)

    if sym_state.get("open") is not None:
        return None
    if not allow_new:
        log("  %s: protection/session/news pause active — no new signal" % cfg["label"])
        return None
    if now - sym_state["last_signal_ts"] <= COOLDOWN_S:
        log("  %s: cooldown active — no new signal" % cfg["label"])
        return None
    if vip and symbol_loss_pause(sym_state, now):
        log("  %s: skipped — recent loss cooldown" % cfg["label"])
        return None
    if vip and st is not None:
        if count_opened_today(st) >= MAX_VIP_PER_DAY:
            log("  %s: skipped — VIP opened-signal daily limit reached" % cfg["label"])
            return None
        if count_opened_today(st, sym) >= MAX_SYMBOL_PER_DAY:
            log("  %s: skipped — symbol opened-signal daily limit reached" % cfg["label"])
            return None

    sig = analyze(sym, candles)
    if not sig:
        log("  %s: no postable HighLow setup — flat" % cfg["label"])
        return None

    score = confidence(sym, sig)
    if vip and score <= MIN_CONF_VIP:
        log("  %s: setup found (HighLow %d/100) <= VIP bar (%d) — skipped"
            % (cfg["label"], score, MIN_CONF_VIP))
        return None

    tr = make_trade(sym, sig, now)
    comps = sig.get("score_components", {})
    rank = score + float(comps.get("structure", 0)) * 0.05 + float(comps.get("momentum", 0)) * 0.05
    return {"symbol": sym, "trade": tr, "rank": rank, "score": score, "sig": sig}


def post_candidate(chat, st, candidate, vip):
    sym = candidate["symbol"]
    ss = st["symbols"].setdefault(sym, new_symbol_state())
    tr = candidate["trade"]
    ss["open"] = tr
    ss["last_signal_ts"] = int(time.time())
    send_telegram(chat, signal_message(sym, tr, vip=vip))
    log("  %s: POSTED best %s signal at %s (HighLow %d/100)"
        % (SYMBOL_CFG[sym]["label"], tr["side"], fmt(sym, tr["entry"]), candidate["score"]))


def run_channel(role):
    chat = VIP_CHAT if role == "vip" else FREE_CHAT
    path = "state-%s.json" % role
    fresh = not os.path.exists(path)
    st = load_state(path)
    if fresh:
        send_telegram(chat, welcome_message(role))

    symbols = SYMBOLS_BY_ROLE.get(role, SYMBOLS_BY_ROLE["free"])
    now = int(time.time())
    vip = (role == "vip")

    allow_new = True
    if vip and global_loss_pause(st, now):
        allow_new = False
        log("VIP protection: last 3 closed VIP trades were losses — pausing new signals temporarily.")
    if vip and not in_session():
        allow_new = False
        log("VIP session filter: outside preferred London trading windows — no new signals.")
    if vip and in_manual_news_pause():
        allow_new = False
        log("VIP news filter: manual high-impact news pause active — no new signals.")

    log("Scanning %d symbol(s) for role '%s'" % (len(symbols), role))
    candidates = []
    for sym in symbols:
        ss = st["symbols"].setdefault(sym, new_symbol_state())
        c = scan_symbol_for_candidate(sym, ss, chat, vip=vip, allow_new=allow_new, st=st)
        if c:
            candidates.append(c)

    # Smart ranking: post only the best setup(s), not every setup.
    if candidates:
        candidates.sort(key=lambda x: x["rank"], reverse=True)
        limit = MAX_POSTS_PER_RUN if vip else 1
        for c in candidates[:limit]:
            post_candidate(chat, st, c, vip=vip)
        for c in candidates[limit:]:
            log("  %s: valid setup skipped — lower rank than posted signal"
                % SYMBOL_CFG[c["symbol"]]["label"])
    else:
        log("No postable setup after HighLow filters/ranking.")

    st["performance"] = performance_stats(st)
    perf = st["performance"]
    log("Performance: %s closed | %dW/%dL | %d%% | %+0.1f pips | last10 %s" % (
        perf["closed"], perf["wins"], perf["losses"], perf["winrate"], perf["pips"], perf["last10"]))
    save_state(st, path)



def collect_stats(role):
    empty = {"signals": 0, "wins": 0, "losses": 0, "winrate": 0,
             "pips": 0.0, "per_symbol": {}}
    try:
        with open("state-%s.json" % role) as f:
            st = json.load(f)
    except Exception:
        return empty
    cutoff = time.time() - 24 * 3600
    trades = []
    for sym, ss in st.get("symbols", {}).items():
        for t in ss.get("closed", []):
            if t.get("closed_ts", 0) >= cutoff:
                tt = dict(t)
                if not tt.get("symbol"):
                    tt["symbol"] = sym
                trades.append(tt)
    if not trades:
        return empty
    wins = [t for t in trades if t["win"]]
    per = {}
    for t in trades:
        per[t["symbol"]] = per.get(t["symbol"], 0) + 1
    return {"signals": len(trades), "wins": len(wins),
            "losses": len(trades) - len(wins),
            "winrate": int(100 * len(wins) / len(trades)),
            "pips": round(sum(t["pips"] for t in trades), 1),
            "per_symbol": per, "trades": trades}


def run_recap():
    vip_s, free_s = collect_stats("vip"), collect_stats("free")
    total_n = vip_s["signals"] + free_s["signals"]
    if total_n == 0:
        log("No signals in the last 24h — posting nothing (normal on a new setup).")
        return
    best = None
    for s in (vip_s, free_s):
        for t in s.get("trades", []):
            if best is None or t["pips"] > best["pips"]:
                best = t
    stats = {"vip": vip_s, "free": free_s, "best": best}
    send_telegram(VIP_CHAT, recap_message(stats, vip=True))
    send_telegram(FREE_CHAT, recap_message(stats, vip=False))


def main():
    log("gold-hl-bot starting — role: %s" % ROLE)
    try:
        if ROLE == "recap":
            run_recap()
        else:
            run_channel(ROLE)
    except Exception as e:
        log("FATAL: %s" % e)
        sys.exit(1)
    log("Done.")


if __name__ == "__main__":
    main()
    
