#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gold-hl-bot — Gold (XAU/USD) High–Low signal bot for Telegram.
Designed to run on GitHub Actions free tier (public repo = free minutes).

Roles (set via env ROLE):
    free  -> analyse & post to the FREE channel  (env TELEGRAM_CHAT_ID)
    vip   -> analyse & post to the VIP channel   (env TELEGRAM_VIP_CHAT_ID)
    recap -> daily summary of the last 24h to both channels

The bot's "memory" (price history + open trade + closed trades) is stored in
state-<role>.json, which the workflow commits back to the repo after each run.

Price data (first one that works is used):
    1. Twelve Data (if TWELVEDATA_KEY secret is set)
    2. Yahoo Finance gold futures (GC=F) — no key needed
    3. gold-api.com spot price — no key needed (history builds up over runs)

If TELEGRAM_BOT_TOKEN is not set, the bot runs in DRY-RUN mode: it prints the
message it would send instead of sending it (useful for testing).
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

HISTORY_MAX = 800            # max price points kept in state
WARMUP      = 60             # points needed before signalling
LOOKBACK    = 60             # high/low detection window
EXCLUDE     = 6              # last N points excluded from the H/L (breakout room)
ATR_POINTS  = 20             # points used for the volatility (ATR) estimate
SL_ATR      = 1.6            # stop-loss distance in ATRs
TP1_ATR     = 2.4            # take-profit-1 distance in ATRs
TP2_ATR     = 4.0            # take-profit-2 distance in ATRs
COOLDOWN_S  = 45 * 60        # min seconds between new signals
GOLD_PIP    = 0.1            # 1 pip on XAU/USD = $0.10 move
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


# ------------------------------------------------------------------- state ----

def new_state():
    return {"history": [],       # [[unix_ts, price], ...] ascending
            "open": None,        # currently open trade (or None)
            "closed": [],        # list of closed trades (last 200)
            "last_signal_ts": 0} # unix ts of last new signal


def load_state(path=None):
    try:
        with open(path or STATE_FILE) as f:
            st = json.load(f)
        for k, v in new_state().items():
            st.setdefault(k, v)
        return st
    except Exception:
        return new_state()


def save_state(st, path=None):
    st["history"] = st["history"][-HISTORY_MAX:]
    st["closed"]  = st["closed"][-200:]
    p = path or STATE_FILE
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)
    log("State saved (%d history points)" % len(st["history"]))


# ------------------------------------------------------------------ prices ----

def merge_history(st, points):
    """Merge freshly fetched [[ts, price], ...] into state history (dedupe)."""
    if not points:
        return
    hist = {int(round(ts)): float(p) for ts, p in st["history"]}
    for ts, price in points:
        if price and price > 0:
            hist[int(round(ts))] = float(price)
    st["history"] = sorted(hist.items())[-HISTORY_MAX:]


def fetch_prices():
    """Return [[unix_ts, price], ...] ascending, newest last. May be short."""
    # 1) Twelve Data (best: real history) — only if a key is configured
    if TD_KEY:
        try:
            d = http_json(
                "https://api.twelvedata.com/time_series"
                "?symbol=XAU/USD&interval=5min&outputsize=200&apikey=%s" % TD_KEY
            )
            vals = d.get("values") or []
            pts = []
            for v in vals:
                t = dt.datetime.fromisoformat(v["datetime"][:19]).replace(tzinfo=TZ)
                pts.append([int(t.timestamp()), float(v["close"])])
            if pts:
                log("Price source: Twelve Data (%d points)" % len(pts))
                return pts
        except Exception as e:
            log("Twelve Data failed: %s" % e)

    # 2) Yahoo Finance gold futures — no key, gives history
    try:
        d = http_json("https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
                      "?interval=5m&range=5d")
        r = d["chart"]["result"][0]
        ts = r.get("timestamp") or []
        cl = (r["indicators"]["quote"][0] or {}).get("close") or []
        pts = [[int(t), float(c)] for t, c in zip(ts, cl) if c]
        if pts:
            log("Price source: Yahoo (GC=F, %d points)" % len(pts))
            return pts
    except Exception as e:
        log("Yahoo failed: %s" % e)

    # 3) gold-api.com spot — no key, current price only
    try:
        d = http_json("https://api.gold-api.com/price/XAU")
        pts = [[int(time.time()), float(d["price"])]]
        log("Price source: gold-api.com spot")
        return pts
    except Exception as e:
        log("gold-api.com failed: %s" % e)

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


def atr_estimate(closes):
    diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    if not diffs:
        return 0.5
    a = sum(diffs[-ATR_POINTS:]) / min(len(diffs), ATR_POINTS)
    return max(a, 0.5)  # floor so SL/TP are never absurdly tight


def analyze(closes):
    """High/Low breakout with trend filter.
    Returns a signal dict or None."""
    if len(closes) < WARMUP:
        return None
    window = closes[-(LOOKBACK + EXCLUDE):-EXCLUDE]
    hi, lo = max(window), min(window)
    last = closes[-1]
    fast, slow = ema(closes[-60:], 12), ema(closes[-60:], 48)
    a = atr_estimate(closes)
    if last > hi and fast > slow:
        return {"side": "BUY", "level": last, "hi": hi, "lo": lo, "atr": a,
                "kind": "breakout above %.2f" % hi}
    if last < lo and fast < slow:
        return {"side": "SELL", "level": last, "hi": hi, "lo": lo, "atr": a,
                "kind": "breakdown below %.2f" % lo}
    return None


def confidence(sig):
    """Rough 55–90% confidence score from trend alignment + breakout margin."""
    base = 62.0
    margin = abs(sig["level"] - (sig["hi"] if sig["side"] == "BUY" else sig["lo"]))
    base += min(margin / max(sig["atr"], 0.01) * 6.0, 22.0)
    return int(min(round(base), 90))


def pips(side, entry, exit_):
    d = (exit_ - entry) if side == "BUY" else (entry - exit_)
    return d / GOLD_PIP


def make_trade(sig, now):
    entry = sig["level"]
    a = sig["atr"]
    if sig["side"] == "BUY":
        sl, tp1, tp2 = entry - SL_ATR * a, entry + TP1_ATR * a, entry + TP2_ATR * a
    else:
        sl, tp1, tp2 = entry + SL_ATR * a, entry - TP1_ATR * a, entry - TP2_ATR * a
    return {"side": sig["side"], "kind": sig["kind"], "entry": round(entry, 2),
            "sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "atr": round(a, 2), "conf": confidence(sig), "opened_ts": now}


# ---------------------------------------------------------------- messages ----

def ts_str(ts):
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%d %b %Y, %H:%M UTC")


def signal_message(tr, vip):
    head = "\U0001F7E1 GOLD SIGNAL — %s %s" % (
        tr["side"], "\u2B06\uFE0F" if tr["side"] == "BUY" else "\u2B07\uFE0F")
    if vip:
        return "\n".join([
            head,
            "\U0001F48E VIP — %s" % tr["kind"],
            "",
            "Entry: %.2f" % tr["entry"],
            "Stop loss: %.2f  (%d pips)" % (tr["sl"], abs(pips(tr["side"], tr["entry"], tr["sl"]))),
            "TP 1: %.2f  (%d pips)" % (tr["tp1"], abs(pips(tr["side"], tr["entry"], tr["tp1"]))),
            "TP 2: %.2f  (%d pips)" % (tr["tp2"], abs(pips(tr["side"], tr["entry"], tr["tp2"]))),
            "",
            "Confidence: %d%%  |  ATR %.2f" % (tr["conf"], tr["atr"]),
            ts_str(tr["opened_ts"]),
        ])
    lines = [
        head,
        "Entry: %.2f  (%s)" % (tr["entry"], tr["kind"]),
        "",
        "Full SL + TP1/TP2 levels, live updates & daily recap \u2192 VIP",
    ]
    if FOOTER:
        lines += ["\U0001F449 Upgrade: %s" % FOOTER]
    lines.append(ts_str(tr["opened_ts"]))
    return "\n".join(lines)


def close_message(tr, exit_price, reason, win, vip=True):
    p = pips(tr["side"], tr["entry"], exit_price)
    emoji = "\u2705" if win else "\u274C"
    return "\n".join([
        "%s CLOSED — %s from %.2f" % (emoji, tr["side"], tr["entry"]),
        "Exit: %.2f (%s)" % (exit_price, reason),
        "Result: %+.0f pips %s" % (p, "\U0001F7E9" if win else "\U0001F5E5"),
        ts_str(int(time.time())),
    ])


def recap_message(stats, vip):
    lines = ["\U0001F4CA DAILY RECAP — %s" %
             dt.datetime.now(TZ).strftime("%d %b %Y"), ""]
    for label, s in (("VIP", stats.get("vip")), ("Free", stats.get("free"))):
        if s and s["signals"]:
            lines.append("%s: %d signals \u00B7 %dW/%dL \u00B7 %d%% \u00B7 %+.0f pips" % (
                label, s["signals"], s["wins"], s["losses"],
                s["winrate"], s["pips"]))
        else:
            lines.append("%s: no closed signals" % label)
    total = stats["total"]
    if total["signals"]:
        b = stats["best"]
        lines += ["", "Best trade: %s %+.0f pips" % (b["side"], b["pips"])]
        if not vip and FOOTER:
            lines.append("Get every signal: %s" % FOOTER)
    return "\n".join(lines)


# ------------------------------------------------------------------- roles ----

def welcome_message(role):
    if role == "vip":
        return "\n".join([
            "\u2705 GOLD HL BOT — VIP channel is LIVE",
            "",
            "Full signals with SL + TP1/TP2 land here every 5 minutes.",
            "Daily recap arrives at 21:05 UTC.",
            ts_str(int(time.time())),
        ])
    lines = [
        "\u2705 GOLD HL BOT — free channel is LIVE",
        "",
        "Signals post here every 15 minutes.",
        "Full SL + TP levels + 5-min signals \u2192 VIP",
    ]
    if FOOTER:
        lines.append("\U0001F449 " + FOOTER)
    lines.append(ts_str(int(time.time())))
    return "\n".join(lines)


def run_channel(role):
    chat = VIP_CHAT if role == "vip" else FREE_CHAT
    path = "state-%s.json" % role          # each role keeps its own memory
    fresh = not os.path.exists(path)       # brand-new setup?
    st = load_state(path)
    if fresh:
        # One-time hello: instantly proves token + chat ID are correct.
        send_telegram(chat, welcome_message(role))
    pts = fetch_prices()
    merge_history(st, pts)
    closes = [p for _, p in st["history"]]

    if len(closes) < 2:
        log("Not enough price data yet — warming up (%d points)." % len(closes))
        save_state(st, path)
        return

    price = closes[-1]
    now = int(time.time())
    log("Gold: %.2f | history: %d points | role: %s" % (price, len(closes), role))

    # 1) manage the open trade first
    tr = st["open"]
    if tr:
        if tr["side"] == "BUY":
            hit_sl, hit_tp = price <= tr["sl"], price >= tr["tp1"]
        else:
            hit_sl, hit_tp = price >= tr["sl"], price <= tr["tp1"]
        if hit_sl or hit_tp:
            reason = "SL" if hit_sl else "TP1"
            exit_price = tr["sl"] if hit_sl else tr["tp1"]
            win = hit_tp
            send_telegram(chat, close_message(tr, exit_price, reason, win))
            st["closed"].append({**tr, "exit": exit_price, "reason": reason,
                                 "win": win, "closed_ts": now,
                                 "pips": round(pips(tr["side"], tr["entry"], exit_price), 1)})
            st["open"] = None
        else:
            log("Open %s from %.2f still running (now %.2f)." % (tr["side"], tr["entry"], price))

    # 2) maybe open a new signal
    if st["open"] is None and now - st["last_signal_ts"] > COOLDOWN_S:
        sig = analyze(closes)
        if sig:
            tr = make_trade(sig, now)
            st["open"] = tr
            st["last_signal_ts"] = now
            send_telegram(chat, signal_message(tr, vip=(role == "vip")))
            log("New %s signal at %.2f." % (tr["side"], tr["entry"]))
        else:
            log("No breakout setup right now — staying flat.")
    save_state(st, path)


def collect_stats(role):
    try:
        with open("state-%s.json" % role) as f:
            st = json.load(f)
    except Exception:
        return {"signals": 0, "wins": 0, "losses": 0, "winrate": 0, "pips": 0.0}
    cutoff = time.time() - 24 * 3600
    trades = [t for t in st.get("closed", []) if t.get("closed_ts", 0) >= cutoff]
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    pips_sum = sum(t["pips"] for t in trades)
    return {"signals": len(trades), "wins": len(wins), "losses": len(losses),
            "winrate": int(100 * len(wins) / len(trades)) if trades else 0,
            "pips": round(pips_sum, 1), "trades": trades}


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
    stats = {"vip": vip_s, "free": free_s, "total": {"signals": total_n}, "best": best}
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
TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
VIP_CHAT    = os.environ.get("TELEGRAM_VIP_CHAT_ID", "").strip()
FREE_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TD_KEY      = os.environ.get("TWELVEDATA_KEY", "").strip()
FOOTER      = os.environ.get("FOOTER", "").strip()

HISTORY_MAX = 800            # max price points kept in state
WARMUP      = 60             # points needed before signalling
LOOKBACK    = 60             # high/low detection window
EXCLUDE     = 6              # last N points excluded from the H/L (breakout room)
ATR_POINTS  = 20             # points used for the volatility (ATR) estimate
SL_ATR      = 1.6            # stop-loss distance in ATRs
TP1_ATR     = 2.4            # take-profit-1 distance in ATRs
TP2_ATR     = 4.0            # take-profit-2 distance in ATRs
COOLDOWN_S  = 45 * 60        # min seconds between new signals
GOLD_PIP    = 0.1            # 1 pip on XAU/USD = $0.10 move
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


# ------------------------------------------------------------------- state ----

def new_state():
    return {"history": [],       # [[unix_ts, price], ...] ascending
            "open": None,        # currently open trade (or None)
            "closed": [],        # list of closed trades (last 200)
            "last_signal_ts": 0} # unix ts of last new signal


def load_state(path=None):
    try:
        with open(path or STATE_FILE) as f:
            st = json.load(f)
        for k, v in new_state().items():
            st.setdefault(k, v)
        return st
    except Exception:
        return new_state()


def save_state(st, path=None):
    st["history"] = st["history"][-HISTORY_MAX:]
    st["closed"]  = st["closed"][-200:]
    p = path or STATE_FILE
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)
    log("State saved (%d history points)" % len(st["history"]))


# ------------------------------------------------------------------ prices ----

def merge_history(st, points):
    """Merge freshly fetched [[ts, price], ...] into state history (dedupe)."""
    if not points:
        return
    hist = {int(round(ts)): float(p) for ts, p in st["history"]}
    for ts, price in points:
        if price and price > 0:
            hist[int(round(ts))] = float(price)
    st["history"] = sorted(hist.items())[-HISTORY_MAX:]


def fetch_prices():
    """Return [[unix_ts, price], ...] ascending, newest last. May be short."""
    # 1) Twelve Data (best: real history) — only if a key is configured
    if TD_KEY:
        try:
            d = http_json(
                "https://api.twelvedata.com/time_series"
                "?symbol=XAU/USD&interval=5min&outputsize=200&apikey=%s" % TD_KEY
            )
            vals = d.get("values") or []
            pts = []
            for v in vals:
                t = dt.datetime.fromisoformat(v["datetime"][:19]).replace(tzinfo=TZ)
                pts.append([int(t.timestamp()), float(v["close"])])
            if pts:
                log("Price source: Twelve Data (%d points)" % len(pts))
                return pts
        except Exception as e:
            log("Twelve Data failed: %s" % e)

    # 2) Yahoo Finance gold futures — no key, gives history
    try:
        d = http_json("https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
                      "?interval=5m&range=5d")
        r = d["chart"]["result"][0]
        ts = r.get("timestamp") or []
        cl = (r["indicators"]["quote"][0] or {}).get("close") or []
        pts = [[int(t), float(c)] for t, c in zip(ts, cl) if c]
        if pts:
            log("Price source: Yahoo (GC=F, %d points)" % len(pts))
            return pts
    except Exception as e:
        log("Yahoo failed: %s" % e)

    # 3) gold-api.com spot — no key, current price only
    try:
        d = http_json("https://api.gold-api.com/price/XAU")
        pts = [[int(time.time()), float(d["price"])]]
        log("Price source: gold-api.com spot")
        return pts
    except Exception as e:
        log("gold-api.com failed: %s" % e)

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


def atr_estimate(closes):
    diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    if not diffs:
        return 0.5
    a = sum(diffs[-ATR_POINTS:]) / min(len(diffs), ATR_POINTS)
    return max(a, 0.5)  # floor so SL/TP are never absurdly tight


def analyze(closes):
    """High/Low breakout with trend filter.
    Returns a signal dict or None."""
    if len(closes) < WARMUP:
        return None
    window = closes[-(LOOKBACK + EXCLUDE):-EXCLUDE]
    hi, lo = max(window), min(window)
    last = closes[-1]
    fast, slow = ema(closes[-60:], 12), ema(closes[-60:], 48)
    a = atr_estimate(closes)
    if last > hi and fast > slow:
        return {"side": "BUY", "level": last, "hi": hi, "lo": lo, "atr": a,
                "kind": "breakout above %.2f" % hi}
    if last < lo and fast < slow:
        return {"side": "SELL", "level": last, "hi": hi, "lo": lo, "atr": a,
                "kind": "breakdown below %.2f" % lo}
    return None


def confidence(sig):
    """Rough 55–90% confidence score from trend alignment + breakout margin."""
    base = 62.0
    margin = abs(sig["level"] - (sig["hi"] if sig["side"] == "BUY" else sig["lo"]))
    base += min(margin / max(sig["atr"], 0.01) * 6.0, 22.0)
    return int(min(round(base), 90))


def pips(side, entry, exit_):
    d = (exit_ - entry) if side == "BUY" else (entry - exit_)
    return d / GOLD_PIP


def make_trade(sig, now):
    entry = sig["level"]
    a = sig["atr"]
    if sig["side"] == "BUY":
        sl, tp1, tp2 = entry - SL_ATR * a, entry + TP1_ATR * a, entry + TP2_ATR * a
    else:
        sl, tp1, tp2 = entry + SL_ATR * a, entry - TP1_ATR * a, entry - TP2_ATR * a
    return {"side": sig["side"], "kind": sig["kind"], "entry": round(entry, 2),
            "sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "atr": round(a, 2), "conf": confidence(sig), "opened_ts": now}


# ---------------------------------------------------------------- messages ----

def ts_str(ts):
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%d %b %Y, %H:%M UTC")


def signal_message(tr, vip):
    head = "\U0001F7E1 GOLD SIGNAL — %s %s" % (
        tr["side"], "\u2B06\uFE0F" if tr["side"] == "BUY" else "\u2B07\uFE0F")
    if vip:
        return "\n".join([
            head,
            "\U0001F48E VIP — %s" % tr["kind"],
            "",
            "Entry: %.2f" % tr["entry"],
            "Stop loss: %.2f  (%d pips)" % (tr["sl"], abs(pips(tr["side"], tr["entry"], tr["sl"]))),
            "TP 1: %.2f  (%d pips)" % (tr["tp1"], abs(pips(tr["side"], tr["entry"], tr["tp1"]))),
            "TP 2: %.2f  (%d pips)" % (tr["tp2"], abs(pips(tr["side"], tr["entry"], tr["tp2"]))),
            "",
            "Confidence: %d%%  |  ATR %.2f" % (tr["conf"], tr["atr"]),
            ts_str(tr["opened_ts"]),
        ])
    lines = [
        head,
        "Entry: %.2f  (%s)" % (tr["entry"], tr["kind"]),
        "",
        "Full SL + TP1/TP2 levels, live updates & daily recap \u2192 VIP",
    ]
    if FOOTER:
        lines += ["\U0001F449 Upgrade: %s" % FOOTER]
    lines.append(ts_str(tr["opened_ts"]))
    return "\n".join(lines)


def close_message(tr, exit_price, reason, win, vip=True):
    p = pips(tr["side"], tr["entry"], exit_price)
    emoji = "\u2705" if win else "\u274C"
    return "\n".join([
        "%s CLOSED — %s from %.2f" % (emoji, tr["side"], tr["entry"]),
        "Exit: %.2f (%s)" % (exit_price, reason),
        "Result: %+.0f pips %s" % (p, "\U0001F7E9" if win else "\U0001F5E5"),
        ts_str(int(time.time())),
    ])


def recap_message(stats, vip):
    lines = ["\U0001F4CA DAILY RECAP — %s" %
             dt.datetime.now(TZ).strftime("%d %b %Y"), ""]
    for label, s in (("VIP", stats.get("vip")), ("Free", stats.get("free"))):
        if s and s["signals"]:
            lines.append("%s: %d signals \u00B7 %dW/%dL \u00B7 %d%% \u00B7 %+.0f pips" % (
                label, s["signals"], s["wins"], s["losses"],
                s["winrate"], s["pips"]))
        else:
            lines.append("%s: no closed signals" % label)
    total = stats["total"]
    if total["signals"]:
        b = stats["best"]
        lines += ["", "Best trade: %s %+.0f pips" % (b["side"], b["pips"])]
        if not vip and FOOTER:
            lines.append("Get every signal: %s" % FOOTER)
    return "\n".join(lines)


# ------------------------------------------------------------------- roles ----

def run_channel(role):
    chat = VIP_CHAT if role == "vip" else FREE_CHAT
    path = "state-%s.json" % role          # each role keeps its own memory
    st = load_state(path)
    pts = fetch_prices()
    merge_history(st, pts)
    closes = [p for _, p in st["history"]]

    if len(closes) < 2:
        log("Not enough price data yet — warming up (%d points)." % len(closes))
        save_state(st, path)
        return

    price = closes[-1]
    now = int(time.time())
    log("Gold: %.2f | history: %d points | role: %s" % (price, len(closes), role))

    # 1) manage the open trade first
    tr = st["open"]
    if tr:
        if tr["side"] == "BUY":
            hit_sl, hit_tp = price <= tr["sl"], price >= tr["tp1"]
        else:
            hit_sl, hit_tp = price >= tr["sl"], price <= tr["tp1"]
        if hit_sl or hit_tp:
            reason = "SL" if hit_sl else "TP1"
            exit_price = tr["sl"] if hit_sl else tr["tp1"]
            win = hit_tp
            send_telegram(chat, close_message(tr, exit_price, reason, win))
            st["closed"].append({**tr, "exit": exit_price, "reason": reason,
                                 "win": win, "closed_ts": now,
                                 "pips": round(pips(tr["side"], tr["entry"], exit_price), 1)})
            st["open"] = None
        else:
            log("Open %s from %.2f still running (now %.2f)." % (tr["side"], tr["entry"], price))

    # 2) maybe open a new signal
    if st["open"] is None and now - st["last_signal_ts"] > COOLDOWN_S:
        sig = analyze(closes)
        if sig:
            tr = make_trade(sig, now)
            st["open"] = tr
            st["last_signal_ts"] = now
            send_telegram(chat, signal_message(tr, vip=(role == "vip")))
            log("New %s signal at %.2f." % (tr["side"], tr["entry"]))
        else:
            log("No breakout setup right now — staying flat.")
    save_state(st, path)


def collect_stats(role):
    try:
        with open("state-%s.json" % role) as f:
            st = json.load(f)
    except Exception:
        return {"signals": 0, "wins": 0, "losses": 0, "winrate": 0, "pips": 0.0}
    cutoff = time.time() - 24 * 3600
    trades = [t for t in st.get("closed", []) if t.get("closed_ts", 0) >= cutoff]
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    pips_sum = sum(t["pips"] for t in trades)
    return {"signals": len(trades), "wins": len(wins), "losses": len(losses),
            "winrate": int(100 * len(wins) / len(trades)) if trades else 0,
            "pips": round(pips_sum, 1), "trades": trades}


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
    stats = {"vip": vip_s, "free": free_s, "total": {"signals": total_n}, "best": best}
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
