#!/usr/bin/env python3
"""Plutus paper-trading engine — one cycle.

Runs twice daily via GitHub Actions. Fetches prices, lets each bot trade,
applies identical fees to all, appends the audit log, writes NAV history.
Fails safe: if data or the LLM is unavailable, it records what it can and
never trades on bad inputs. Zero third-party dependencies.
"""
import json
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DRY_RUN = "--dry" in sys.argv  # fetch + decide but don't write files


# ---------- IO ----------

def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


def save(name, obj):
    if DRY_RUN:
        return
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f, indent=1)


def log(msg):
    print(f"[plutus] {msg}", flush=True)


# ---------- Price fetch ----------

def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def fetch_finnhub(symbols):
    prices = {}
    for sym in symbols:
        try:
            raw = http_get(
                f"https://finnhub.io/api/v1/quote?symbol={sym}&token={FINNHUB_KEY}"
            )
            c = json.loads(raw).get("c")
            if c and c > 0:
                prices[sym] = round(float(c), 4)
        except (urllib.error.URLError, ValueError, KeyError) as e:
            log(f"finnhub {sym}: {e}")
    return prices


def fetch_yahoo(symbols):
    """Keyless fallback via Yahoo's v8 chart endpoint. ~15 min delayed."""
    prices = {}
    ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    for sym in symbols:
        try:
            raw = http_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                "?interval=1d&range=1d", headers=ua)
            meta = json.loads(raw)["chart"]["result"][0]["meta"]
            p = meta.get("regularMarketPrice")
            if p and p > 0:
                prices[sym] = round(float(p), 4)
        except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError) as e:
            log(f"yahoo {sym}: {e}")
    return prices


def fetch_prices(universe):
    source = "finnhub"
    prices = fetch_finnhub(universe) if FINNHUB_KEY else {}
    missing = [s for s in universe if s not in prices]
    if missing:
        source = "finnhub+yahoo" if prices else "yahoo"
        prices.update(fetch_yahoo(missing))
    return prices, source


# ---------- Portfolio math ----------

def nav_of(bot_state, prices):
    nav = bot_state["cash"]
    for sym, qty in bot_state["positions"].items():
        if sym in prices:
            nav += qty * prices[sym]
    return round(nav, 4)


def execute(bot, bot_state, order, prices, rules, ts, trades, source):
    """Apply one validated order with fees+slippage. Returns True if filled."""
    sym, action = order["symbol"], order["action"]
    notional = float(order["notional"])
    price = prices[sym]
    slip = rules["slippage_bps_per_side"] / 10000.0
    fee_rate = rules["fee_bps_per_side"] / 10000.0

    if action == "buy":
        exec_price = price * (1 + slip)
        qty = notional / exec_price
        fee = notional * fee_rate
        cost = notional + fee
        if cost > bot_state["cash"] + 1e-9:
            return False
        bot_state["cash"] -= cost
        bot_state["positions"][sym] = bot_state["positions"].get(sym, 0.0) + qty
    else:  # sell
        held_qty = bot_state["positions"].get(sym, 0.0)
        exec_price = price * (1 - slip)
        qty = min(notional / exec_price, held_qty)
        if qty <= 1e-9:
            return False
        proceeds = qty * exec_price
        fee = proceeds * fee_rate
        bot_state["cash"] += proceeds - fee
        remaining = held_qty - qty
        if remaining * price < 0.01:
            bot_state["positions"].pop(sym, None)
        else:
            bot_state["positions"][sym] = remaining

    trades.append({
        "ts": ts, "bot": bot, "action": action, "symbol": sym,
        "qty": round(qty, 6), "price": price, "exec_price": round(exec_price, 4),
        "fee": round(fee, 4), "notional": round(qty * exec_price, 2),
        "rationale": order.get("rationale", ""), "source": source,
    })
    return True


def validate_orders(orders, bot_state, prices, rules, universe):
    """Clamp/reject orders against the pre-registered hard rules."""
    valid, nav = [], nav_of(bot_state, prices)
    cash_after = bot_state["cash"]
    min_cash = rules["min_cash_buffer_pct_nav"] * nav
    max_pos = rules["max_position_pct_nav"] * nav
    for o in orders:
        try:
            sym = str(o["symbol"]).upper()
            action = str(o["action"]).lower()
            notional = float(o["notional"])
        except (KeyError, TypeError, ValueError):
            continue
        if sym not in universe or sym not in prices or notional <= 0:
            continue
        if action == "sell":
            held_val = bot_state["positions"].get(sym, 0.0) * prices[sym]
            if held_val < 0.01:
                continue
            o = {**o, "symbol": sym, "action": action,
                 "notional": min(notional, held_val)}
            cash_after += o["notional"]
            valid.append(o)
        elif action == "buy":
            pos_val = bot_state["positions"].get(sym, 0.0) * prices[sym]
            allowed = min(notional, max_pos - pos_val, cash_after - min_cash)
            if allowed < 1.0:
                continue
            o = {**o, "symbol": sym, "action": action, "notional": round(allowed, 2)}
            cash_after -= allowed
            valid.append(o)
    # Sells first so buys can use the freed cash.
    valid.sort(key=lambda o: 0 if o["action"] == "sell" else 1)
    return valid


def rebalance_to_targets(bot_state, targets, prices, rules):
    """Produce orders that move a mechanical bot to equal-weight targets."""
    nav = nav_of(bot_state, prices)
    budget = nav * (1 - rules["min_cash_buffer_pct_nav"] - 0.01)
    per_name = min(budget / max(len(targets), 1), rules["max_position_pct_nav"] * nav)
    orders = []
    for sym, qty in list(bot_state["positions"].items()):
        if sym not in targets and sym in prices:
            orders.append({"action": "sell", "symbol": sym,
                           "notional": qty * prices[sym] * 1.001,
                           "rationale": "rebalance: exited target set"})
    for sym in targets:
        if sym not in prices:
            continue
        cur = bot_state["positions"].get(sym, 0.0) * prices[sym]
        gap = per_name - cur
        if gap > max(5.0, 0.01 * nav):
            orders.append({"action": "buy", "symbol": sym, "notional": round(gap, 2),
                           "rationale": "rebalance: toward equal weight"})
        elif gap < -max(5.0, 0.01 * nav):
            orders.append({"action": "sell", "symbol": sym, "notional": round(-gap, 2),
                           "rationale": "rebalance: trim overweight"})
    return orders


# ---------- Bots ----------

def bot_voo(bot_state, prices, rules, first_cycle, **_):
    if first_cycle and "VOO" in prices and not bot_state["positions"]:
        nav = nav_of(bot_state, prices)
        budget = bot_state["cash"] * (1 - rules["fee_bps_per_side"] / 10000.0) - \
            rules["min_cash_buffer_pct_nav"] * nav
        return [{"action": "buy", "symbol": "VOO", "notional": round(budget, 2),
                 "rationale": "Buy the market once, then do nothing for a month."}]
    return []


def bot_momentum(bot_state, prices, rules, history, first_run_today, cfg,
                 universe, **_):
    if not first_run_today:
        return []
    lookback = cfg["bots"]["momentum"]["lookback_snapshots"]
    top_n = cfg["bots"]["momentum"]["top_n"]
    if len(history) < 2:
        return []  # not enough history yet: stay in cash
    past = history[-min(lookback, len(history))]["prices"]
    returns = {s: prices[s] / past[s] - 1
               for s in universe if s in prices and past.get(s, 0) > 0}
    if len(returns) < top_n:
        return []
    targets = sorted(returns, key=returns.get, reverse=True)[:top_n]
    return rebalance_to_targets(bot_state, targets, prices, rules)


def bot_random(bot_state, prices, rules, first_run_today, ts, cfg,
               universe, **_):
    if not first_run_today:
        return []
    n = cfg["bots"]["random"]["positions"]
    seed = int(ts[:10].replace("-", ""))  # YYYYMMDD → deterministic per day
    rng = random.Random(seed)
    candidates = sorted(s for s in universe if s in prices)
    if len(candidates) < n:
        return []
    targets = rng.sample(candidates, n)
    return rebalance_to_targets(bot_state, targets, prices, rules)


# ---------- Plutus (the LLM) ----------

def call_opus(system, user_msg, model):
    """One Messages API call with server-side web search enabled.

    The API runs Plutus's searches internally; we get back the final
    content blocks. Returns the list of text blocks in order.
    """
    body = json.dumps({
        "model": model,
        "max_tokens": 6000,
        "system": system,
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 6,
        }],
        "messages": [{"role": "user", "content": user_msg}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.loads(r.read().decode())
    return [b.get("text", "") for b in resp.get("content", [])
            if b.get("type") == "text" and b.get("text")]


def parse_decision(blocks):
    """The decision JSON lives in the last text block (after any research
    narration); fall back to scanning all blocks joined."""
    for text in [blocks[-1] if blocks else "", "\n".join(blocks)]:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(text[start:end + 1])
            except ValueError:
                continue
            if isinstance(obj.get("orders", []), list):
                return obj
    raise ValueError("no valid decision JSON in response")


def bot_plutus(bot_state, prices, rules, history, trades_log, ts, cfg, **_):
    if not ANTHROPIC_KEY:
        log("plutus: no ANTHROPIC_API_KEY — holding")
        return []
    with open(os.path.join(ROOT, cfg["bots"]["plutus"]["charter"])) as f:
        charter = f.read()

    nav = nav_of(bot_state, prices)
    recent_hist = [
        {"ts": h["ts"], "prices": h["prices"]} for h in history[-10:]
    ]
    my_trades = [t for t in trades_log if t["bot"] == "plutus"][-12:]
    user_msg = json.dumps({
        "timestamp_utc": ts,
        "rules": rules,
        "your_cash": round(bot_state["cash"], 2),
        "your_positions": {
            s: {"qty": round(q, 4), "value": round(q * prices.get(s, 0), 2)}
            for s, q in bot_state["positions"].items()
        },
        "your_nav": nav,
        "watchlist_prices": prices,
        "note": ("watchlist_prices is a convenience, not a cage: you may order "
                 "any US-listed stock or ETF by ticker. Unknown symbols are "
                 "quoted live at execution; if no quote exists the order is "
                 "rejected and logged."),
        "recent_price_snapshots": recent_hist,
        "your_recent_trades": my_trades,
    }, indent=1)

    model = cfg["bots"]["plutus"]["model"]
    for attempt in (1, 2):
        try:
            blocks = call_opus(charter, user_msg, model)
            decision = parse_decision(blocks)
            view = decision.get("market_view", "").strip()
            if view:
                log(f"plutus view: {view}")
                append_view(ts, view)
            return decision.get("orders", [])
        except (urllib.error.URLError, ValueError, KeyError) as e:
            log(f"plutus attempt {attempt} failed: {e}")
    log("plutus: giving up this cycle — holding")
    return []


def append_view(ts, view):
    path = os.path.join(DATA, "views.json")
    views = []
    if os.path.exists(path):
        with open(path) as f:
            views = json.load(f)
    views.append({"ts": ts, "view": view})
    if not DRY_RUN:
        with open(path, "w") as f:
            json.dump(views, f, indent=1)


# ---------- Main cycle ----------

def main():
    cfg = load("config.json")
    portfolio = load("portfolio.json")
    trades = load("trades.json")
    nav_history = load("nav_history.json")
    history = load("prices.json")
    rules = cfg["rules"]
    universe = sorted(sum(cfg["universe"].values(), []))
    # Plutus may hold names outside the core watchlist — keep pricing them.
    extras = sorted(s for s in portfolio["bots"]["plutus"]["positions"]
                    if s not in universe)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = ts[:10]

    prices, source = fetch_prices(universe + extras)
    coverage = sum(1 for s in universe if s in prices) / len(universe)
    log(f"{len(prices)}/{len(universe) + len(extras)} prices via {source}")
    if coverage < 0.8:
        log("ABORT: <80% price coverage — recording nothing, trading nothing")
        return 0
    for s in extras:  # unquotable held extra: mark at last known price
        if s not in prices and history:
            stale = history[-1]["prices"].get(s)
            if stale:
                prices[s] = stale
                log(f"{s}: no live quote, marked at last known {stale}")

    trading_days = {h["ts"][:10] for h in history}
    first_run_today = today not in trading_days
    first_cycle = len(history) == 0
    experiment_over = (
        len(trading_days | {today}) > cfg["experiment"]["max_trading_days"]
        and not first_cycle
    )

    if portfolio["start_date"] is None:
        portfolio["start_date"] = today

    history.append({"ts": ts, "prices": prices, "source": source})

    bots = {
        "voo": bot_voo,
        "momentum": bot_momentum,
        "random": bot_random,
        "plutus": bot_plutus,
    }
    for bot, decide in bots.items():
        state = portfolio["bots"][bot]
        if experiment_over:
            orders = []
        else:
            orders = decide(
                bot_state=state, prices=prices, rules=rules, history=history,
                trades_log=trades, ts=ts, cfg=cfg, universe=universe,
                first_cycle=first_cycle, first_run_today=first_run_today,
            )
            if bot == "plutus" and orders:
                # Open universe: quote any symbols we don't have yet.
                unknown = sorted({
                    str(o.get("symbol", "")).upper() for o in orders
                    if isinstance(o, dict)
                } - set(prices))
                unknown = [s for s in unknown if 0 < len(s) <= 6 and
                           all(c.isalnum() or c in ".-" for c in s)]
                if unknown:
                    fetched, _ = fetch_prices(unknown)
                    prices.update(fetched)  # also lands in today's snapshot
                    rejected = set(unknown) - set(fetched)
                    if rejected:
                        log(f"plutus: no quote for {sorted(rejected)} — rejected")
            allowed = set(prices) if bot == "plutus" else universe
            orders = validate_orders(orders, state, prices, rules, allowed)
        filled = sum(
            execute(bot, state, o, prices, rules, ts, trades, source)
            for o in orders
        )
        nav = nav_of(state, prices)
        nav_history.append({"ts": ts, "bot": bot, "nav": nav})
        log(f"{bot}: {filled} fills, NAV ${nav:.2f}")

    if experiment_over:
        log(f"experiment complete ({cfg['experiment']['max_trading_days']} "
            "trading days) — NAV recorded, no further trades")

    save("portfolio.json", portfolio)
    save("trades.json", trades)
    save("nav_history.json", nav_history)
    save("prices.json", history)
    log("cycle done" + (" (dry run — nothing written)" if DRY_RUN else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
