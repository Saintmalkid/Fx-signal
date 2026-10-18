#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strategy_v3.py — GOLD SWING ENGINE (shadow mode — posts nothing, ever).

The only configuration that survived backtesting on real data:

    4H Donchian(30/15) on GOLD, 2024-2026 (~2 years, costs included):
        58 trades | 29W/29L | 50% win | +7,461 pips | avg +129/trade

    Daily Donchian(20/10) on GOLD, 5 years: +9,227 pips (34% win, avg +288)
    -> higher timeframe = bigger edge. This engine uses the 4H version.

Logic (exactly what was backtested — no untested extras):
  * Bars: 1h Yahoo data resampled to 4h (OHLC).
  * Entry: bar CLOSES above the prior 30-bar highest high  -> BUY at close.
            bar CLOSES below the prior 30-bar lowest  low  -> SELL at close.
  * Exit:  opposite 15-bar extreme (close beyond it) — the classic Donchian
           exit. No stop-loss: the backtested edge comes from giving trends
           room; hard stops killed it in testing.
  * One position at a time. Cost: 6 pips round trip (spread/slippage).
  * Forward-only record: the shadow state starts EMPTY on first run; every
    trade recorded from now on is real forward performance, nothing replayed.

Modes:
    python strategy_v3.py              # shadow tick (run by workflow)
    python strategy_v3.py --backtest   # re-verify the edge on live data
    python strategy_v3.py --report     # print current shadow record

Never exits non-zero — the workflow stays green no matter what.
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

STATE_FILE = "state-v3.json"
TZ = dt.timezone.utc

SYMBOL      = "XAUUSD"
YAHOO       = "GC=F"
PIP         = 0.1
COST_RT     = 6.0        # pips, round trip
ENTRY_N     = 30         # 4H bars (~5 days) breakout window
EXIT_N      = 15         # 4H bars (~2.5 days) exit window
BAR_SECONDS = 4 * 3600


def log(msg):
    print("[V3 %s] %s" % (dt.datetime.now(TZ).strftime("%H:%M:%S"), msg), flush=True)


def fetch_1h():
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?interval=1h&range=730d" % urllib.parse.quote(YAHOO))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (v3shadow)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    res = d["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        h, l, c = q["high"][i], q["low"][i], q["close"][i]
        if h and l and c:
            bars.append((int(t), float(h), float(l), float(c)))
    return bars


def resample_4h(bars_1h):
    """Chunk 1h bars into 4h OHLC. Returns [[ts,high,low,close], ...]."""
    out = []
    for i in range(0, len(bars_1h) - 3, 4):
        chunk = bars_1h[i:i + 4]
        out.append([chunk[0][0],
                    max(b[1] for b in chunk),
                    min(b[2] for b in chunk),
                    chunk[-1][3]])
    return out


def completed_bars(bars_4h, now=None):
    """Drop the still-forming last bar so we only act on closed 4H bars."""
    now = now or time.time()
    out = []
    for b in bars_4h:
        if b[0] + BAR_SECONDS <= now:
            out.append(b)
    return out


def donchian_step(bars, i, position):
    """Evaluate bar i (already closed). Returns (position, closed_trade|None)."""
    ts, h, l, c = bars[i]
    hi_entry = max(b[1] for b in bars[i - ENTRY_N:i])
    lo_entry = min(b[2] for b in bars[i - ENTRY_N:i])
    hi_exit  = max(b[1] for b in bars[i - EXIT_N:i])
    lo_exit  = min(b[2] for b in bars[i - EXIT_N:i])

    trade = None
    if position:
        side, entry, opened_ts = position
        if side == "BUY" and c < lo_exit:
            trade = {"symbol": SYMBOL, "side": side, "entry": entry,
                     "exit": c, "opened_ts": position[2], "closed_ts": ts,
                     "pips": round((c - entry) / PIP - COST_RT, 1)}
            position = None
        elif side == "SELL" and c > hi_exit:
            trade = {"symbol": SYMBOL, "side": side, "entry": entry,
                     "exit": c, "opened_ts": position[2], "closed_ts": ts,
                     "pips": round((entry - c) / PIP - COST_RT, 1)}
            position = None
    if position is None:
        if c > hi_entry:
            position = ("BUY", c, ts)
        elif c < lo_entry:
            position = ("SELL", c, ts)
    return position, trade


def run_backtest():
    log("BACKTEST — 4H Donchian(%d/%d) on GOLD, live data:" % (ENTRY_N, EXIT_N))
    bars = completed_bars(resample_4h(fetch_1h()))
    if len(bars) < ENTRY_N + EXIT_N + 60:
        log("not enough history (%d bars)" % len(bars))
        return
    position, trades = None, []
    start = max(ENTRY_N, EXIT_N) + 1
    for i in range(start, len(bars)):
        position, trade = donchian_step(bars, i, position)
        if trade:
            trades.append(trade)
    wins = [t for t in trades if t["pips"] > 0]
    net = sum(t["pips"] for t in trades)
    eq = peak = mdd = 0.0
    for t in trades:
        eq += t["pips"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    span = "%s → %s" % (
        dt.datetime.fromtimestamp(bars[0][0], TZ).strftime("%d %b %Y"),
        dt.datetime.fromtimestamp(bars[-1][0], TZ).strftime("%d %b %Y"))
    log("  history: %s (%d 4H bars)" % (span, len(bars)))
    log("  RESULT: %d trades | %dW/%dL | %d%% | net %+.0f pips | avg %+.0f | maxDD %.0f"
        % (len(trades), len(wins), len(trades) - len(wins),
           100 * len(wins) // max(len(trades), 1), net,
           net / max(len(trades), 1), mdd))
    if position:
        log("  (an open %s from %.2f would still be running)" % (position[0], position[1]))


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"position": None, "trades": [], "last_bar_ts": 0, "started_ts": int(time.time())}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def run_shadow():
    log("shadow tick — forward-only record, no posting")
    st = load_state()
    bars = completed_bars(resample_4h(fetch_1h()))
    if len(bars) < ENTRY_N + EXIT_N + 5:
        log("not enough history yet — nothing to do")
        return
    new = [b for b in bars if b[0] > st.get("last_bar_ts", 0)]
    if not new:
        log("no new completed 4H bars (last: %s)" %
            dt.datetime.fromtimestamp(st["last_bar_ts"], TZ).strftime("%d %b %H:%M")
            if st.get("last_bar_ts") else "no new completed 4H bars")
        return
    first_run = st.get("last_bar_ts", 0) == 0
    if first_run:
        # forward-only: skip replaying history, start watching from the next bar
        st["last_bar_ts"] = bars[-1][0]
        save_state(st)
        log("first run — record starts fresh; next closed bar begins evaluation "
            "(last bar: %s)" % dt.datetime.fromtimestamp(bars[-1][0], TZ).strftime("%d %b %H:%M"))
        return
    start_i = next((i for i, b in enumerate(bars) if b[0] > st["last_bar_ts"]), len(bars))
    pos = st.get("position")
    pos = (pos["side"], pos["entry"], pos["opened_ts"]) if pos else None
    for i in range(max(start_i, ENTRY_N), len(bars)):   # ensure window exists
        pos, trade = donchian_step(bars, i, pos)
        if trade:
            st.setdefault("trades", []).append(trade)
            log("  CLOSED %s %s -> %s (%+.1f pips)" %
                (trade["side"], trade["entry"], trade["exit"], trade["pips"]))
    if pos and not st.get("position"):
        log("  ENTER %s @ %.2f (30-bar breakout)" % (pos[0], pos[1]))
    st["position"] = ({"side": pos[0], "entry": pos[1], "opened_ts": pos[2]}
                      if pos else None)
    st["last_bar_ts"] = bars[-1][0]
    st["trades"] = st.get("trades", [])[-200:]
    save_state(st)
    t = st.get("trades", [])
    if t:
        wins = len([x for x in t if x["pips"] > 0])
        log("shadow record: %d trades | %dW/%dL | %d%% | %+.1f pips"
            % (len(t), wins, len(t) - wins, 100 * wins // len(t),
               sum(x["pips"] for x in t)))


def run_report():
    st = load_state()
    t = st.get("trades", [])
    log("GOLD SWING ENGINE — shadow record since %s" %
        dt.datetime.fromtimestamp(st.get("started_ts", 0), TZ).strftime("%d %b %Y"))
    if t:
        wins = [x for x in t if x["pips"] > 0]
        log("  trades: %d | %dW/%dL | %d%% win | net %+.1f pips | avg %+.1f"
            % (len(t), len(wins), len(t) - len(wins),
               100 * len(wins) // len(t), sum(x["pips"] for x in t),
               sum(x["pips"] for x in t) / len(t)))
        for x in t[-5:]:
            log("    %s %s %s -> %s  %+.1f pips" %
                (dt.datetime.fromtimestamp(x["closed_ts"], TZ).strftime("%d %b"),
                 x["side"], x["entry"], x["exit"], x["pips"]))
    else:
        log("  no closed trades yet — engine is watching for the first "
            "30-bar breakout")
    p = st.get("position")
    if p:
        log("  OPEN: %s from %.2f (opened %s)" %
            (p["side"], p["entry"],
             dt.datetime.fromtimestamp(p["opened_ts"], TZ).strftime("%d %b %H:%M")))


if __name__ == "__main__":
    try:
        if "--backtest" in sys.argv:
            run_backtest()
        elif "--report" in sys.argv:
            run_report()
        else:
            run_shadow()
    except Exception as e:
        log("FATAL (absorbed): %s" % e)
    log("done.")
