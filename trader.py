#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trader.py — Deriv demo-account EXECUTION module for gold-hl-bot.

Runs right after bot.py in the VIP workflow. It reads the bot's state
(state-vip.json): every open signal the bot posted to the VIP channel gets
mirrored as a REAL trade on the Deriv DEMO account (virtual money, zero
risk). When the bot closes a signal (TP/SL), the Deriv position is closed
too. This builds a verified, auditable track record.

Design rules:
  * Never fails the workflow — every error is logged and we exit 0.
  * Runs in IDLE mode (does nothing) until DERIV_TOKEN secret is set.
  * Multiplier contracts: loss is capped at the stake (no martingale!).
  * Own memory in state-deriv.json (committed by the same workflow step).

Env (all optional except DERIV_TOKEN):
  DERIV_TOKEN       API token from Deriv (scopes: read + trade)
  DERIV_STAKE       stake per trade in account currency      (default 5)
  DERIV_MULTIPLIER  contract multiplier                      (default 50)
  DERIV_APP_ID      Deriv app id                             (default 1089)
"""

import datetime as dt
import json
import os
import sys
import time

TOKEN    = os.environ.get("DERIV_TOKEN", "").strip()
STAKE    = float(os.environ.get("DERIV_STAKE", "5"))
MULT     = int(os.environ.get("DERIV_MULTIPLIER", "100"))
APP_ID   = os.environ.get("DERIV_APP_ID", "1089")
WS_URL   = "wss://ws.derivws.com/websockets/v3?app_id=%s" % APP_ID

BOT_STATE_FILE    = "state-vip.json"
TRADER_STATE_FILE = "state-deriv.json"

# bot symbol -> Deriv symbol (multiplier contracts)
SYMBOL_MAP = {
    "XAUUSD": "frXXAUUSD",
    "EURUSD": "frxEURUSD",
    "GBPUSD": "frxGBPUSD",
    "USDJPY": "frxUSDJPY",
}

TZ = dt.timezone.utc

def log(msg):
    print("[TRADER %s] %s" % (dt.datetime.now(TZ).strftime("%H:%M:%S"), msg), flush=True)

# ------------------------------------------------------------------ state ----

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save_state(st):
    tmp = TRADER_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, TRADER_STATE_FILE)

# ------------------------------------------------------------- deriv api -----

class DerivAPI:
    """Very small synchronous wrapper over the Deriv WebSocket API."""

    def __init__(self, url=WS_URL):
        self.url = url
        self.ws = None

    def connect(self):
        from websocket import create_connection   # imported lazily
        self.ws = create_connection(self.url, timeout=25)
        log("Connected to Deriv API")

    def request(self, payload):
        self.ws.send(json.dumps(payload))
        while True:                                # skip pings if any
            resp = json.loads(self.ws.recv())
            if resp.get("msg_type") != "ping":
                return resp

    def authorize(self, token):
        r = self.request({"authorize": token})
        if r.get("error"):
            raise RuntimeError("authorize failed: %s" % r["error"]["message"])
        acct = r.get("authorize", {})
        log("Authorized — account: %s (%s %s, %s)" % (
            acct.get("loginid"), acct.get("currency", ""),
            acct.get("balance", "?"), acct.get("is_virtual") == "1" and "DEMO" or "REAL"))
        return acct

    def balance(self):
        r = self.request({"balance": 1})
        b = r.get("balance", {})
        return float(b.get("balance", 0)), b.get("currency", "?")

    def buy_multiplier(self, deriv_sym, side, stake, mult, sl_amount, tp_amount):
        ct = "MULTUP" if side == "BUY" else "MULTDOWN"
        req = {
            "buy": 1,
            "price": round(stake, 2),
            "parameters": {
                "amount": round(stake, 2),
                "basis": "stake",
                "contract_type": ct,
                "currency": "USD",
                "symbol": deriv_sym,
                "multiplier": mult,
                "limit_order": {
                    "stop_loss": round(sl_amount, 2),
                    "take_profit": round(tp_amount, 2),
                },
            },
        }
        r = self.request(req)
        if r.get("error"):
            raise RuntimeError("buy failed: %s" % r["error"]["message"])
        b = r.get("buy", {})
        log("BOUGHT %s %s x%d stake %.2f -> contract %s (SL -%.2f / TP +%.2f USD)"
            % (ct, deriv_sym, mult, stake, b.get("contract_id"), sl_amount, tp_amount))
        return b

    def sell(self, contract_id):
        r = self.request({"sell": contract_id, "price": 0})
        if r.get("error"):
            raise RuntimeError("sell failed: %s" % r["error"]["message"])
        s = r.get("sell", {})
        log("SOLD contract %s for %.2f (payout)" % (contract_id, float(s.get("sold_for", 0))))
        return s

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

# ---------------------------------------------------------------- logic ------

def take_profit_amount(trade, stake, mult):
    """Approx USD profit if price hits the bot's TP1, capped at 4x stake."""
    try:
        move = abs(float(trade["tp1"]) - float(trade["entry"]))
        entry = abs(float(trade["entry"]))
        amt = stake * mult * (move / entry)
        return max(0.5, min(amt, stake * 4))
    except Exception:
        return stake * 2

def run(api_factory=None):
    if not TOKEN:
        log("IDLE — no DERIV_TOKEN secret set. Execution module disabled (signals still post normally).")
        return 0

    bot_state = load_json(BOT_STATE_FILE, {})
    opens = {}
    for sym, ss in (bot_state.get("symbols") or {}).items():
        tr = ss.get("open")
        if tr and tr.get("side") in ("BUY", "SELL"):
            opens[sym] = tr

    tstate = load_json(TRADER_STATE_FILE, {"open": {}, "history": []})
    tstate.setdefault("open", {})
    tstate.setdefault("history", [])

    try:
        api = (api_factory or DerivAPI)()
        api.connect()
    except Exception as e:
        log("Cannot reach Deriv (%s) — skipping this run. Workflow continues." % e)
        return 0

    try:
        acct = api.authorize(TOKEN)
        bal, cur = api.balance()
        log("Demo balance: %.2f %s | bot has %d open signal(s): %s"
            % (bal, cur, len(opens), ", ".join(opens) or "-"))

        # 1) close contracts whose bot signal no longer exists
        for sym in list(tstate["open"].keys()):
            if sym not in opens:
                rec = tstate["open"].pop(sym)
                try:
                    api.sell(rec["contract_id"])
                    rec["closed_ts"] = int(time.time())
                    tstate["history"] = (tstate["history"] + [rec])[-200:]
                except Exception as e:
                    log("Could not sell %s contract %s: %s — will retry next run"
                        % (sym, rec.get("contract_id"), e))
                    tstate["open"][sym] = rec          # keep for retry

        # 2) open contracts for new bot signals
        for sym, tr in opens.items():
            if sym in tstate["open"]:
                continue                               # already executed
            dsym = SYMBOL_MAP.get(sym)
            if not dsym:
                log("No Deriv symbol mapped for %s — skipped" % sym)
                continue
            tp_amt = take_profit_amount(tr, STAKE, MULT)
            try:
                b = api.buy_multiplier(dsym, tr["side"], STAKE, MULT,
                                       sl_amount=STAKE, tp_amount=tp_amt)
                tstate["open"][sym] = {
                    "contract_id": b.get("contract_id"),
                    "buy_price": float(b.get("buy_price", 0)),
                    "side": tr["side"], "entry": tr["entry"],
                    "bot_conf": tr.get("conf"), "stake": STAKE,
                    "multiplier": MULT, "opened_ts": int(time.time()),
                }
            except Exception as e:
                log("Could not buy %s (%s) — will retry next run" % (sym, e))

        save_state(tstate)
        log("Execution state: %d open contract(s), %d historical"
            % (len(tstate["open"]), len(tstate["history"])))
    except Exception as e:
        log("Deriv execution error: %s — workflow continues (signals unaffected)" % e)
    finally:
        api.close()
    return 0


if __name__ == "__main__":
    log("trader starting")
    sys.exit(run())
                              
