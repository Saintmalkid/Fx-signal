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
WARMUP      = 120            # safer mode needs more history before signalling
LOOKBACK    = 60             # high/low detection window
EXCLUDE     = 6              # last N points excluded from the H/L (breakout room)
ATR_POINTS  = 20             # points used for the volatility estimate
SL_ATR      = 2.2            # tighter controlled risk than the older 2.6 ATR
TP1_ATR     = 3.2            # take-profit-1 distance in ATRs
TP2_ATR     = 5.0            # take-profit-2 distance in ATRs
COOLDOWN_S  = 90 * 60        # minimum time between new signals per symbol

# -------------------------- VIP safety controls -----------------------------
# The old version allowed too many fake breakouts. This version is stricter:
# - default VIP minimum is 89, and code posts only ABOVE this, so only 90% posts
# - pauses a symbol after a loss
# - pauses all new VIP signals after a losing streak
# - rejects choppy, weak-trend, and over-extended breakouts
MIN_CONF_VIP       = int(os.environ.get("VIP_MIN_CONF", "89"))
LOSS_COOLDOWN_S    = int(os.environ.get("LOSS_COOLDOWN_HOURS", "6")) * 3600
GLOBAL_LOSS_PAUSE_S = int(os.environ.get("GLOBAL_LOSS_PAUSE_HOURS", "4")) * 3600
MAX_VIP_PER_DAY    = int(os.environ.get("MAX_VIP_SIGNALS_PER_DAY", "5"))
MAX_SYMBOL_PER_DAY = int(os.environ.get("MAX_SYMBOL_SIGNALS_PER_DAY", "1"))
MAX_SPREAD_ATR     = float(os.environ.get("MAX_BREAKOUT_ATR", "2.8"))
MIN_BREAK_ATR      = float(os.environ.get("MIN_BREAKOUT_ATR", "0.35"))
MIN_EFFICIENCY     = float(os.environ.get("MIN_TREND_EFFICIENCY", "0.28"))
TZ          = dt.timezone.utc

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
    return {"history": [], "open": None, "closed": [], "last_signal_ts": 0}


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
    migrated["symbols"]["XAUUSD"] = {
        "history": raw.get("history", []),
        "open": raw.get("open"),
        "closed": raw.get("closed", []),
        "last_signal_ts": raw.get("last_signal_ts", 0),
    }
    log("Migrated legacy gold-only state into multi-symbol format.")
    return migrated


def save_state(st, path=None):
    for sym, ss in st["symbols"].items():
        ss["history"] = ss["history"][-HISTORY_MAX:]
        ss["closed"]  = ss["closed"][-200:]
    p = path or STATE_FILE
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)
    total = sum(len(ss["history"]) for ss in st["symbols"].values())
    log("State saved (%d history points across %d symbols)"
        % (total, len(st["symbols"])))

# ------------------------------------------------------------------ prices ----

def merge_history(sym_state, points):
    if not points:
        return
    hist = {int(round(ts)): float(p) for ts, p in sym_state["history"]}
    for ts, price in points:
        if price and price > 0:
            hist[int(round(ts))] = float(price)
    sym_state["history"] = sorted(hist.items())[-HISTORY_MAX:]


def fetch_prices(sym):
    """Return [[unix_ts, price], ...] ascending, newest last. May be empty."""
    cfg = SYMBOL_CFG[sym]
    if TD_KEY:
        try:
            d = http_json(
                "https://api.twelvedata.com/time_series"
                "?symbol=%s&interval=5min&outputsize=200&apikey=%s"
                % (urllib.parse.quote(cfg["td"]), TD_KEY)
            )
            vals = d.get("values") or []
            pts = []
            for v in vals:
                t = dt.datetime.fromisoformat(v["datetime"][:19]).replace(tzinfo=TZ)
                pts.append([int(t.timestamp()), float(v["close"])])
            if pts:
                log("  %s: Twelve Data (%d pts)" % (cfg["label"], len(pts)))
                return pts
        except Exception as e:
            log("  %s: Twelve Data failed: %s" % (cfg["label"], e))
    try:
        d = http_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                      "%s?interval=5m&range=5d" % urllib.parse.quote(cfg["yahoo"]))
        r = d["chart"]["result"][0]
        ts = r.get("timestamp") or []
        cl = (r["indicators"]["quote"][0] or {}).get("close") or []
        pts = [[int(t), float(c)] for t, c in zip(ts, cl) if c]
        if pts:
            log("  %s: Yahoo (%d pts)" % (cfg["label"], len(pts)))
            return pts
    except Exception as e:
        log("  %s: Yahoo failed: %s" % (cfg["label"], e))
    if cfg.get("gold_api"):
        try:
            d = http_json("https://api.gold-api.com/price/%s" % cfg["gold_api"])
            pts = [[int(time.time()), float(d["price"])]]
            log("  %s: gold-api.com spot" % cfg["label"])
            return pts
        except Exception as e:
            log("  %s: gold-api failed: %s" % (cfg["label"], e))
    return []

# ---------------------------------------------------------------- strategy ----

def ema(values, period):
    if not values:
        return None
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr_estimate(sym, closes):
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


def recent_momentum_ok(side, closes, a):
    """Require the last few candles to still support the signal direction."""
    if len(closes) < 8:
        return False
    m3 = closes[-1] - closes[-4]
    m6 = closes[-1] - closes[-7]
    if side == "BUY":
        return m3 > 0.10 * a and m6 > 0.20 * a
    return m3 < -0.10 * a and m6 < -0.20 * a


def analyze(sym, closes):
    """Safer breakout strategy. Returns signal dict or None.

    The earlier version rewarded very large breakouts, which can enter late and
    get caught by reversals. This version only accepts clean breakouts with:
      - EMA trend alignment on two speeds
      - enough trend efficiency, so chop/ranges are skipped
      - breakout distance not too small and not too extended
      - recent momentum still moving in the signal direction
    """
    if len(closes) < WARMUP:
        return None

    window = closes[-(LOOKBACK + EXCLUDE):-EXCLUDE]
    hi, lo = max(window), min(window)
    last, prev = closes[-1], closes[-2]
    fast, slow = ema(closes[-60:], 12), ema(closes[-60:], 48)
    mid_fast, mid_slow = ema(closes[-120:], 20), ema(closes[-120:], 80)
    a = atr_estimate(sym, closes)
    er = efficiency_ratio(closes, 30)

    # Skip wild one-candle spikes; these often reverse immediately.
    last_jump_atr = abs(last - prev) / max(a, 1e-9)
    if last_jump_atr > 1.8:
        return None

    trend_gap_atr = abs(fast - slow) / max(a, 1e-9)
    htf_gap_atr = abs(mid_fast - mid_slow) / max(a, 1e-9)

    # GOLD is noisier and produced recent losses, so make it stricter.
    er_min = MIN_EFFICIENCY + (0.08 if sym == "XAUUSD" else 0.0)
    trend_min = 0.30 + (0.15 if sym == "XAUUSD" else 0.0)

    if er < er_min or trend_gap_atr < trend_min or htf_gap_atr < 0.20:
        return None

    def build(side, margin, ref_level, kind):
        margin_atr = margin / max(a, 1e-9)
        if margin_atr < MIN_BREAK_ATR or margin_atr > MAX_SPREAD_ATR:
            return None
        if not recent_momentum_ok(side, closes, a):
            return None
        # Quality score: trend + efficiency + clean breakout. Capped at 90.
        score = 70.0
        score += min(er * 18.0, 12.0)
        score += min(trend_gap_atr * 12.0, 8.0)
        score += min(htf_gap_atr * 8.0, 5.0)
        # Sweet spot: a confirmed breakout, but not late/exhausted.
        sweet = 1.2
        score += max(0.0, 5.0 - abs(margin_atr - sweet) * 2.2)
        if sym == "XAUUSD":
            score -= 2.0
        return {"side": side, "level": last, "hi": hi, "lo": lo, "atr": a,
                "conf": int(min(round(score), 90)), "er": round(er, 3),
                "trend_gap": round(trend_gap_atr, 3), "margin_atr": round(margin_atr, 3),
                "kind": kind % fmt(sym, ref_level)}

    if last > hi and fast > slow and mid_fast > mid_slow:
        return build("BUY", last - hi, hi, "clean breakout above %s")
    if last < lo and fast < slow and mid_fast < mid_slow:
        return build("SELL", lo - last, lo, "clean breakdown below %s")
    return None


def confidence(sym, sig):
    """Return the strategy quality score, already calculated by analyze()."""
    if "conf" in sig:
        return int(sig["conf"])
    base = 62.0
    margin = abs(sig["level"] - (sig["hi"] if sig["side"] == "BUY" else sig["lo"]))
    base += min(margin / max(sig["atr"], 1e-9) * 6.0, 28.0)
    return int(min(round(base), 90))


def make_trade(sym, sig, now):
    entry = sig["level"]
    a = sig["atr"]
    if sig["side"] == "BUY":
        sl, tp1, tp2 = entry - SL_ATR * a, entry + TP1_ATR * a, entry + TP2_ATR * a
    else:
        sl, tp1, tp2 = entry + SL_ATR * a, entry - TP1_ATR * a, entry - TP2_ATR * a
    return {"symbol": sym, "side": sig["side"], "kind": sig["kind"],
            "entry": round(entry, 6), "sl": round(sl, 6),
            "tp1": round(tp1, 6), "tp2": round(tp2, 6),
            "atr": round(a, 6), "conf": confidence(sym, sig), "opened_ts": now}

# ---------------------------------------------------------------- messages ----

def ts_str(ts):
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%d %b %Y, %H:%M UTC")


def signal_message(sym, tr, vip):
    cfg = SYMBOL_CFG[sym]
    head = "%s %s SIGNAL — %s %s" % (
        cfg["emoji"], cfg["label"], tr["side"],
        "\u2B06\uFE0F" if tr["side"] == "BUY" else "\u2B07\uFE0F")
    if vip:
        return "\n".join([
            head,
            "\U0001F48E VIP — %s" % tr["kind"],
            "",
            "Entry: %s" % fmt(sym, tr["entry"]),
            "Stop loss: %s  (%d pips)" % (fmt(sym, tr["sl"]), abs(pips_for(sym, tr["side"], tr["entry"], tr["sl"]))),
            "TP 1: %s  (%d pips)" % (fmt(sym, tr["tp1"]), abs(pips_for(sym, tr["side"], tr["entry"], tr["tp1"]))),
            "TP 2: %s  (%d pips)" % (fmt(sym, tr["tp2"]), abs(pips_for(sym, tr["side"], tr["entry"], tr["tp2"]))),
            "",
            "Confidence: %d%%" % tr["conf"],
            ts_str(tr["opened_ts"]),
        ])
    lines = [
        head,
        "Entry: %s  (%s)" % (fmt(sym, tr["entry"]), tr["kind"]),
        "",
        "Full SL + TP1/TP2 levels, live updates & daily recap \u2192 VIP",
    ]
    if FOOTER:
        lines.append("\U0001F449 Upgrade: %s" % FOOTER)
    lines.append(ts_str(tr["opened_ts"]))
    return "\n".join(lines)


def close_message(sym, tr, exit_price, reason, win):
    cfg = SYMBOL_CFG[sym]
    p = pips_for(sym, tr["side"], tr["entry"], exit_price)
    emoji = "\u2705" if win else "\u274C"
    return "\n".join([
        "%s %s CLOSED — %s from %s" % (emoji, cfg["label"], tr["side"], fmt(sym, tr["entry"])),
        "Exit: %s (%s)" % (fmt(sym, exit_price), reason),
        "Result: %+.0f pips %s" % (p, "\U0001F7E9" if win else "\U0001F5E5"),
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


def run_symbol(sym, sym_state, chat, vip, allow_new=True, all_trades=None):
    """Manage one symbol: update history, manage open trade, maybe signal."""
    cfg = SYMBOL_CFG[sym]
    pts = fetch_prices(sym)
    merge_history(sym_state, pts)
    closes = [p for _, p in sym_state["history"]]

    if len(closes) < 2:
        log("  %s: warming up (%d pts)" % (cfg["label"], len(closes)))
        return

    price = closes[-1]
    now = int(time.time())
    log("  %s: %s | %d pts" % (cfg["label"], fmt(sym, price), len(closes)))

    # 1) manage the open trade first
    tr = sym_state["open"]
    if tr:
        if tr["side"] == "BUY":
            hit_sl, hit_tp = price <= tr["sl"], price >= tr["tp1"]
        else:
            hit_sl, hit_tp = price >= tr["sl"], price <= tr["tp1"]
        if hit_sl or hit_tp:
            reason = "SL" if hit_sl else "TP1"
            exit_price = tr["sl"] if hit_sl else tr["tp1"]
            win = hit_tp
            send_telegram(chat, close_message(sym, tr, exit_price, reason, win))
            sym_state["closed"].append({
                **tr, "symbol": sym, "exit": exit_price, "reason": reason, "win": win,
                "closed_ts": now,
                "pips": round(pips_for(sym, tr["side"], tr["entry"], exit_price), 1)})
            sym_state["open"] = None
        else:
            log("  %s: open %s from %s still running (now %s)"
                % (cfg["label"], tr["side"], fmt(sym, tr["entry"]), fmt(sym, price)))

    # 2) safety gates before opening a new trade
    if sym_state["open"] is not None:
        return
    if not allow_new:
        log("  %s: VIP protection pause active — no new signal" % cfg["label"])
        return
    if now - sym_state["last_signal_ts"] <= COOLDOWN_S:
        log("  %s: cooldown active — no new signal" % cfg["label"])
        return
    if vip and symbol_loss_pause(sym_state, now):
        log("  %s: skipped — recent loss cooldown" % cfg["label"])
        return
    if vip and all_trades is not None:
        if count_trades_today(all_trades) >= MAX_VIP_PER_DAY:
            log("  %s: skipped — VIP daily signal limit reached" % cfg["label"])
            return
        if count_trades_today(all_trades, sym) >= MAX_SYMBOL_PER_DAY:
            log("  %s: skipped — symbol daily limit reached" % cfg["label"])
            return

    # 3) maybe open a new signal
    sig = analyze(sym, closes)
    if sig:
        conf = confidence(sym, sig)
        if vip and conf <= MIN_CONF_VIP:
            # VIP quality bar: found a setup, but it is not strong enough.
            log("  %s: setup found (%d%% conf) <= VIP bar (%d%%) — skipped"
                % (cfg["label"], conf, MIN_CONF_VIP))
        else:
            tr = make_trade(sym, sig, now)
            sym_state["open"] = tr
            sym_state["last_signal_ts"] = now
            send_telegram(chat, signal_message(sym, tr, vip=vip))
            log("  %s: NEW %s signal at %s (%d%% conf)"
                % (cfg["label"], tr["side"], fmt(sym, tr["entry"]), conf))
    else:
        log("  %s: no clean safe setup — flat" % cfg["label"])


def run_channel(role):
    chat = VIP_CHAT if role == "vip" else FREE_CHAT
    path = "state-%s.json" % role
    fresh = not os.path.exists(path)
    st = load_state(path)
    if fresh:
        # One-time hello: instantly proves token + chat ID are correct.
        send_telegram(chat, welcome_message(role))
    symbols = SYMBOLS_BY_ROLE.get(role, SYMBOLS_BY_ROLE["free"])
    now = int(time.time())
    vip = (role == "vip")
    all_trades = closed_trades_from_state(st)
    allow_new = True
    if vip and global_loss_pause(st, now):
        allow_new = False
        log("VIP protection: last 3 closed trades were losses — pausing new signals temporarily.")
    log("Scanning %d symbol(s) for role '%s'" % (len(symbols), role))
    for sym in symbols:
        ss = st["symbols"].setdefault(sym, new_symbol_state())
        run_symbol(sym, ss, chat, vip=vip, allow_new=allow_new, all_trades=all_trades)
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
    
