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
| **Copycat** | Mirrors a famous claimed-best portfolio (default: the Pelosi tracker — Autopilot's $400M flagship) under real copier conditions: public-disclosure lag, shares only. Tests whether "the best portfolio online" survives being copied. |
| **Maleen** | The human benchmark: the owner's own picks (NVDA/RKLB/TSM 2:2:2, VOO 4), pre-registered at cycle 2, buy-and-hold. The owner vs their own AI. |

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
- **Scheduler:** a Claude Code **routine** (scheduled cloud agent) runs twice
  daily (`30 14,19 * * 1-5` UTC, ~10:30 & ~15:30 ET). The routine agent *is*
  Plutus: it reads the charter, researches the web with its own tools, writes
  `plutus_decision.json`, runs the engine (which validates + executes all four
  bots), and pushes the updated data. The GitHub Actions workflow remains as a
  manual fallback only.
- **Database:** the repo itself. `data/*.json` is committed every cycle — the git
  history is a tamper-evident audit trail of prices, trades, and rationales.
- **Dashboard:** [`index.html`](index.html) on GitHub Pages. Equity curves,
  metrics (split by statistical trustworthiness), holdings, Plutus's journal,
  and a Monte-Carlo "monkey test."

## Setup (one time)

1. Push this repo to GitHub; **Settings → Pages** → deploy from `main`, root.
2. Create the Claude Code routine (twice daily, cron `30 14,19 * * 1-5`) with
   the Plutus run instructions — see the routine prompt in this repo's setup
   notes. In the routine's settings, **allow unrestricted branch pushes** so it
   can commit `data/` to `main` (Pages serves from `main`).
3. Optional: `FINNHUB_API_KEY` in the routine environment for real-time quotes;
   the engine falls back to Yahoo's delayed keyless data without it.

No Anthropic API key is needed — Plutus's thinking runs on the routine itself.

## The cycle, step by step (what the routine does)

```sh
python3 scripts/run_cycle.py --quote   # 1. briefing: prices, holdings, rules
# 2. the agent researches the web, then writes plutus_decision.json
python3 scripts/run_cycle.py           # 3. engine validates + executes all bots
git add data/ && git commit && git push  # 4. publish the audit trail
```

## Local dry run

```sh
python3 scripts/run_cycle.py --dry   # fetches prices, decides, writes nothing
```

## The plan

The full research-backed experiment design — prior art, architecture decisions,
statistical caveats — is in [`plutus-plan.html`](plutus-plan.html).
