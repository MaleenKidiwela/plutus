# Plutus.

**Can a language model trade?** A one-month, pre-registered paper-trading
experiment. An LLM agent — persona authored by Fable 5, decisions executed by
Claude Opus 4.8 — manages $1,000 of imaginary money, twice a day, against three
baselines under identical rules.

> **Not investment advice.** Simulated fills, imaginary money, and a sample size
> the plan itself calls statistically meaningless. This is an experiment in *how
> an LLM decides*, not evidence of edge.

## The contestants

| Bot | Logic |
|---|---|
| **Plutus** | Claude Opus 4.8, guided by [`agents/PLUTUS_CHARTER.md`](agents/PLUTUS_CHARTER.md). Researches the live web each cycle, may trade **any US-listed stock or ETF**, and must justify every trade in writing. Its goal: make money. |
| **VOO Buy & Hold** | Buys the market on day one. Never trades again. |
| **Momentum Bot** | Ranks the universe by trailing return, holds the top 5, rebalances daily. |
| **Random Monkey** | Picks 5 names at random (seeded by date), rebalances daily. The luck control. |

All bots share the same $1,000, cadence, hard rules, and costs (5 bps fee +
5 bps slippage per side). The 27-name universe binds the mechanical baselines
and serves as Plutus's priced watchlist — but Plutus itself may roam the whole
US market (an accepted fairness asymmetry: its brief is to make money, not to
win a controlled comparison). Rules are frozen in
[`data/config.json`](data/config.json) — pre-registered before the first trade.
Trading auto-stops after 21 trading days.

## How it works

- **Engine:** [`scripts/run_cycle.py`](scripts/run_cycle.py) — zero dependencies.
  Fetches prices (Finnhub, Yahoo fallback), lets each bot trade, validates every
  order against the hard rules, appends the audit log.
- **Scheduler:** GitHub Actions cron, `30 14,19 * * 1-5` UTC (~10:30 & ~15:30 ET).
- **Database:** the repo itself. `data/*.json` is committed every cycle — the git
  history is a tamper-evident audit trail of prices, trades, and rationales.
- **Dashboard:** [`index.html`](index.html) on GitHub Pages. Equity curves,
  metrics (split by statistical trustworthiness), holdings, Plutus's journal,
  and a Monte-Carlo "monkey test."

## Setup (one time)

1. Create a GitHub repo and push this directory to `main`.
2. **Settings → Pages** → deploy from branch `main`, root.
3. **Settings → Secrets and variables → Actions** → add:
   - `ANTHROPIC_API_KEY` — for Plutus's decisions
   - `FINNHUB_API_KEY` — free at [finnhub.io](https://finnhub.io) (optional;
     falls back to Yahoo's delayed keyless data)
4. **Actions** tab → enable workflows. The `trading-cycle` workflow can be
   triggered manually with *Run workflow* to test.

Note: GitHub pauses cron schedules after 60 days without repo activity — the
twice-daily commits keep it alive, but if you fork this, kick it off manually.

## Local dry run

```sh
python3 scripts/run_cycle.py --dry   # fetches prices, decides, writes nothing
```

## The plan

The full research-backed experiment design — prior art, architecture decisions,
statistical caveats — is in [`plutus-plan.html`](plutus-plan.html).
