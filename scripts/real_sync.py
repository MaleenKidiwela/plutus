#!/usr/bin/env python3
"""Real-account tracker — the "maleen" contestant mirrors the owner's real
Robinhood account (since 2026-11-04), alongside a shadow VOO.

Shadow VOO: every dollar the real account moves into (or out of) the market
is mirrored as the same dollar amount of VOO at the same moment. "Did my
picks beat simply buying VOO with the same money on the same days?"

The tracker never trades. Real trades go through Robinhood with the owner's
approval; this script only records fills that already happened, in
data/maleen_ledger.json (the source of truth — account numbers never go in).

Holdings are published in real dollars. The NAV series is a time-weighted
index: it equals the account's dollar value until the first deposit after
inception, then stays continuous across deposits so new money never shows
up as a "gain" on the scoreboard.

  real_sync.py deposit AMOUNT [--ts TS]
  real_sync.py fill SYMBOL buy|sell QTY PRICE --voo VOO_PRICE [--ts TS] [--note TEXT]
  real_sync.py publish          # rewrite data/ from the ledger
  real_sync.py mark             # fetch prices, append a NAV point for both
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cycle as engine  # noqa: E402  (reuse IO + price fetch)

LEDGER = "maleen_ledger.json"
BOTS = ("maleen", "shadow_voo")
REAL = BOTS[0]


def load_ledger():
    path = os.path.join(engine.DATA, LEDGER)
    if not os.path.exists(path):
        return {"inception": None, "events": []}
    return engine.load(LEDGER)


def value(state, marks):
    return state["cash"] + sum(q * marks.get(s, 0)
                               for s, q in state["positions"].items())


def replay(events):
    """Rebuild both dollar states, the trade log, and each bot's index factor.

    index = factor × dollar value. The factor starts at 1 and is reset on
    each later deposit so the index is continuous across the cash flow.
    """
    st = {b: {"cash": 0.0, "positions": {}} for b in BOTS}
    factor = {}
    marks, trades = {}, []
    for e in events:
        if e["type"] == "deposit":
            marks.update(e.get("prices", {}))  # live marks at the deposit
            for b in BOTS:
                before = value(st[b], marks)
                st[b]["cash"] += e["amount"]
                factor[b] = 1.0 if b not in factor else \
                    factor[b] * before / value(st[b], marks)
        elif e["type"] == "fill":
            sym, side, qty, px, voo = (e["symbol"], e["side"], e["qty"],
                                       e["price"], e["voo_price"])
            sign = 1 if side == "buy" else -1
            notional = qty * px
            marks[sym], marks["VOO"] = px, voo
            for b, s, q, p, why in (
                    (REAL, sym, qty, px, e.get("note", "")),
                    ("shadow_voo", "VOO", notional / voo, voo,
                     f"mirror of real {side} {sym}: same dollars into VOO")):
                pos = st[b]["positions"]
                st[b]["cash"] -= sign * notional
                pos[s] = pos.get(s, 0.0) + sign * q
                if pos[s] <= 1e-9:
                    pos.pop(s)
                trades.append({
                    "ts": e["ts"], "bot": b, "action": side, "symbol": s,
                    "qty": round(q, 6), "price": p, "exec_price": p, "fee": 0.0,
                    "notional": round(q * p, 2), "rationale": why,
                    "source": "robinhood-fill",
                })
    return st, factor, trades, marks


def publish(led, nav_point=None):
    st, factor, trades, _ = replay(led["events"])
    portfolio = engine.load("portfolio.json")
    for b in BOTS:
        portfolio["bots"][b] = {
            "cash": round(st[b]["cash"], 6),
            "positions": {s: round(q, 6) for s, q in st[b]["positions"].items()},
            "index_factor": factor.get(b, 1.0),
        }
    engine.save("portfolio.json", portfolio)

    # The ledger is the source for the tracked bots' trades: replace, not append.
    others = [t for t in engine.load("trades.json") if t["bot"] not in BOTS]
    engine.save("trades.json", others + trades)

    if nav_point is not None:
        ts, prices = nav_point
        nav = engine.load("nav_history.json")
        for b in BOTS:
            nav.append({"ts": ts, "bot": b, "nav": round(
                factor.get(b, 1.0) * value(st[b], prices), 4)})
        engine.save("nav_history.json", nav)
    return st


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deposit")
    d.add_argument("amount", type=float)
    d.add_argument("--ts", default=None)
    f = sub.add_parser("fill")
    f.add_argument("symbol")
    f.add_argument("side", choices=["buy", "sell"])
    f.add_argument("qty", type=float)
    f.add_argument("price", type=float)
    f.add_argument("--voo", type=float, required=True,
                   help="VOO price at the fill moment (for the shadow)")
    f.add_argument("--ts", default=None)
    f.add_argument("--note", default="")
    sub.add_parser("publish")
    sub.add_parser("mark")
    a = ap.parse_args()

    led = load_ledger()
    if a.cmd == "deposit":
        ts = a.ts or now()
        led["inception"] = led["inception"] or ts
        st, _, _, _ = replay(led["events"])
        syms = sorted({s for b in BOTS for s in st[b]["positions"]})
        prices = engine.fetch_prices(syms)[0] if syms else {}
        if set(syms) - set(prices):
            engine.log(f"deposit: no quote for {sorted(set(syms) - set(prices))}"
                       " — not recorded (index would jump)")
            return 1
        led["events"].append({"type": "deposit", "ts": ts, "amount": a.amount,
                              "prices": prices})
        engine.save(LEDGER, led)
        # NAV point at the deposit: both bots start from the same dollars.
        _, _, _, marks = replay(led["events"])
        publish(led, nav_point=(ts, marks))
    elif a.cmd == "fill":
        led["events"].append({"type": "fill", "ts": a.ts or now(),
                              "symbol": a.symbol.upper(), "side": a.side,
                              "qty": a.qty, "price": a.price,
                              "voo_price": a.voo, "note": a.note})
        engine.save(LEDGER, led)
        # No NAV point here: mid-sequence marks are inconsistent (earlier
        # fills sit at stale prices). The next `mark` or cycle records it.
        publish(led)
    elif a.cmd == "publish":
        publish(led)
    elif a.cmd == "mark":
        st, _, _, _ = replay(led["events"])
        syms = sorted({s for b in BOTS for s in st[b]["positions"]})
        prices, source = engine.fetch_prices(syms)
        missing = sorted(set(syms) - set(prices))
        if missing:
            engine.log(f"mark: no quote for {missing} — nothing recorded")
            return 1
        publish(led, nav_point=(now(), prices))
        engine.log(f"mark: NAV recorded via {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
