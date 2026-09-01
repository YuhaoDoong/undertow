**English** · [中文](README.zh-CN.md)

# undertow

undertow is a dependency-free Python toolkit for examining institutional positioning and derivatives structure beneath gold, silver, WTI crude, and US-dollar price moves. It combines public CFTC and CBOE data, futures and macro context, deterministic analytics, event studies, and self-contained HTML reports.

> **Research use only.** undertow produces structured market scenarios, not price forecasts or trading instructions. Its outputs depend on delayed public data, proxy instruments, model assumptions, and heuristic classification. Use them as evidence for further analysis, never as a substitute for independent risk management.

## What it does

| Layer | Purpose | Main outputs |
|---|---|---|
| Positioning | Reconstruct CFTC participant structure and weekly changes | Net positions, crowding, flow quality, concentration, divergences |
| Options structure | Analyse delayed CBOE ETF-option chains | OI walls, put/call ratio, estimated GEX, zero-gamma level |
| Options flow | Compare snapshots and identify unusual activity | Volume/OI activity, change in OI and IV, inferred build/unwind pressure |
| Macro and events | Add rates, dollar, inflation, volatility, and calendar context | Macro backdrop, event windows, live/previous/forecast metadata |
| Validation | Evaluate historical signal behaviour without look-ahead | Forward-return event studies and confidence estimates |
| Reporting | Combine the layers into an auditable scenario | Markdown/JSON output and a self-contained HTML report with SVG charts |

The LLM, when used, orchestrates commands and interprets structured output. All calculations remain deterministic Python code.

## Design goals

- **Zero runtime dependencies:** the application uses the Python standard library.
- **Public-data first:** core workflows use official or publicly accessible endpoints and do not require API keys.
- **Auditable assumptions:** proxy conversions, lag, GEX sign conventions, and flow heuristics are stated explicitly.
- **Modular architecture:** collection, analysis, and reporting are separated by one-way dependencies.
- **Agent-ready:** `SKILL.md` documents how to expose the command-line interface to Codex or Claude Code.

## Quick start

```bash
git clone https://github.com/YuhaoDoong/undertow.git
cd undertow

python3 -m undertow list
python3 -m undertow analyze gold --json
python3 -m undertow gamma gold --json
python3 -m undertow snapshot gold
python3 -m undertow flow gold --json
python3 -m undertow report gold
```

Generated reports are written under `data/reports/`. The HTML output is self-contained and can be opened directly in a browser.

### Selected commands

```bash
# Positioning
python3 -m undertow analyze
python3 -m undertow analyze gold silver --lookback 104

# Options structure and flow
python3 -m undertow gamma gold --horizon 30
python3 -m undertow snapshot
python3 -m undertow flow --no-snapshot
python3 -m undertow expiry silver

# Macro, events, and volatility
python3 -m undertow calendar gold --within 30
python3 -m undertow vol gold

# Historical validation and integrated reporting
python3 -m undertow backtest gold --horizons 5 10 20
python3 -m undertow report gold --json
```

Use `python3 -m undertow --help` or a subcommand's `--help` for the current options.

## Supported instruments

| Key | Market | Positioning source | Options proxy | Futures reference |
|---|---|---|---|---|
| `gold` | Gold | CFTC Disaggregated COT | GLD | GC=F |
| `silver` | Silver | CFTC Disaggregated COT | SLV | SI=F |
| `wti` | WTI crude | CFTC Disaggregated COT | USO | CL=F |
| `dxy` | US Dollar Index | CFTC Legacy COT | — | DX=F |
| `qqq` | Nasdaq-100 proxy | CFTC Legacy COT | QQQ | NQ=F |

Instrument definitions live in `config/instruments.json`; adding an instrument should normally require configuration plus a compatible collector, not changes throughout the analysis layer.

## Architecture

```text
Public data + brokerage (read-only)
        ↓
undertow.collect      fetch, normalise, cache, persist snapshots
        ↓
undertow.core         shared models, configuration, clock, calendar
        ↓
undertow.analyze      deterministic indicators, signals, scenarios, position review
        ↓
undertow.report       terminal, JSON, self-contained HTML
        ↓
undertow.consult      model-agnostic context packet + local read-only HTTP API
undertow.soul         the trader's own rules, plans, and journal (private)
```

### Module map

```text
undertow/
├── core/                    shared domain models, config, clock, event calendar
│
├── collect/                 ── data layer (one file per source) ──
│   ├── cftc_cot             CFTC COT positioning (Disaggregated / Legacy)
│   ├── cboe_options         option chains via ETF proxies (GLD/SLV/USO/QQQ)
│   ├── cboe_history         daily bars · cboe_vol  GVZ/OVX/VXSLV
│   ├── yahoo_futures        real futures prices (GC=F/SI=F/CL=F/NQ=F)
│   ├── fred_macro           real rates, dollar, breakeven inflation
│   ├── faireconomy_cal      economic calendar feed (forecast/previous/impact)
│   ├── longbridge_account   live positions, assets, cash-flow, executions  [read-only]
│   ├── longbridge_quote     real-time ETF sessions + option last/IV        [read-only]
│   ├── longbridge_news      instrument-specific news headlines             [read-only]
│   └── store · cache        snapshot archive (committed) · TTL cache
│
├── analyze/                 ── deterministic analytics (no I/O) ──
│   ├── positioning・signals    net positions, crowding, smart-money divergence,
│   │                           short-squeeze setup (concentration × directional shorts)
│   ├── gamma・flow・expiry_ladder   OI walls, GEX, zero-gamma; buyer/seller flow via
│   │                           ΔOI × delta-adjusted ΔIV; per-expiry slices
│   ├── macro・volregime・vrp_history   macro backdrop, vol regime, variance risk premium
│   ├── outlook・verdict        weighted multi-factor vote with near/mid horizon split;
│   │                           daily decision synthesis (short? chase? swing? core?)
│   ├── fibonacci・risk_reward  swing legs, retracements, risk-reward gate
│   ├── technicals              MA structure, RSI/KDJ/MACD/Bollinger → overheat score
│   ├── stretch                 overbought/oversold on two orthogonal ATR-normalised
│   │                           axes — distance from MA20 ("how far from normal") and
│   │                           drawdown from the 60-day high ("how far off the top").
│   │                           Their rank series correlate only 0.73, so combining
│   │                           them helps; 20-day drawdown and range-position correlate
│   │                           0.91/0.95 with the first axis and are deliberately left out.
│   │                           Every reading carries the backtested edge, win rate, n and
│   │                           Welch t for its (band × regime) cell — no uncalibrated labels
│   ├── stretch_backtest        regenerates that calibration: local detrending,
│   │                           within-regime comparison, non-overlapping Welch t
│   │                           (`undertow backtest-stretch --emit --compare`)
│   ├── strategy_hub・strategy・condor・credit_spread   scenario parameterisation
│   ├── portfolio               live-position review: combo recognition (verticals,
│   │                           straddles, iron condors, calendars), book stance,
│   │                           capital constraints, live/BS valuation
│   ├── healthcheck             graded risk checks + three entry gates
│   │                           (seller edge / buyer edge / single-long sigma & delta),
│   │                           after-fee expected value
│   ├── newsfeed                news + upcoming high-impact events
│   ├── event_impact            cross-instrument snapshots before/after data releases
│   └── backtest・blackscholes  look-ahead-free event study · minimal BS helpers
│
├── report/                  markdown · html · viz (SVG, no JS)
│
├── consult/                 ── AI interface layer ──
│   ├── packet               deterministic context packet + ready-to-feed prompt
│   └── server               localhost read-only HTTP API (no write endpoints)
│
└── soul/                    ── the trader, not the market (private data) ──
    ├── profile              rules, limits, known weaknesses, lessons, triggers,
    │                        open questions; deterministic discipline checks
    ├── plan                 planned trades: triggers, exits, order parameters
    └── journal              trade log with fees + ex-ante thesis scoring

config/                      instruments, calendar, soul template
scripts/                     daily_update.sh · event_watch.sh (launchd)
tests/                       28 network-free test files
data/snapshots/              option-chain history (committed — not re-fetchable)
data/history/events/         event-impact snapshots (committed)
data/account/ · data/soul/   private: never committed (gitignored)
```

Each package contains a README describing its responsibilities. Dependencies flow one way:
`collect → core → analyze → report/consult/soul`. Analysis never imports collectors.

## Data sources

| Data | Source | Typical limitation |
|---|---|---|
| COT positioning | CFTC public reporting | Weekly; released Friday for Tuesday positions |
| ETF options | CBOE delayed quote endpoints | Delayed and proxy-based; no historical chain archive |
| Futures references | Yahoo Finance chart endpoint | Availability and schema are not guaranteed |
| Macro series | FRED CSV endpoints | Daily or lower frequency; revisions are possible |
| Volatility indices | CBOE | Delayed/end-of-day depending on series |
| Economic calendar | Public FairEconomy feed plus local anchors | Event metadata can change and should be source-checked |

## Validation

The offline suite currently contains 410 tests covering parsing, time alignment, expiry handling, positioning, gamma, flow, backtesting, scenario logic, report generation, the validation registry, and the wall-spread module.

Every judgement that can influence a trading decision must be registered in `undertow/analyze/validation.py` with its sample size, hit count, and p-value. A registry entry may legitimately carry `p_value=None` with status `无法检验` ("cannot be tested") — for example the three core flow gates, whose calibration would require historical per-strike open-interest deltas that free data sources do not provide. Absence of a test is recorded as such rather than filled in with a placeholder.

```bash
python3 -m pytest -q
```

The runtime itself has no third-party dependencies; `pytest` is only needed for development.

## Important assumptions and limitations

- COT data are slow-moving and published with a multi-day lag; they are unsuitable for intraday timing.
- GLD, SLV, and USO options are proxies for commodity derivatives. Converting ETF strikes to futures prices is approximate, especially for USO versus WTI.
- Estimated GEX depends on an assumed dealer-position sign. Actual dealer inventories are not observed.
- Flow direction is inferred from changes in open interest and relative implied volatility, not from tick-level buyer/seller identification.
- Option-chain history must be accumulated locally by running `snapshot`; public delayed endpoints do not provide a complete historical chain.
- Backtests and confidence estimates describe the sampled history and can fail under new market regimes.
- **Snapshot capture time is not yet part of the backtest clock.** Snapshot files are named by date, but capture time has varied: of 191 snapshots, 21 were taken after 09:30 ET and 18 after the close. Backtests still treat every snapshot as known before that day's open, so a look-ahead component remains. Fixing this requires deriving a decision session from `captured_at` rather than from the filename.
- **"Wall" is currently defined as the maximum accumulated open interest inside a band around spot.** Because a maximum always exists inside any band, this can label a minor strike as a wall; widening the band instead surfaces far-dated LEAPS clusters. Both failure modes are live. The intended fix is to separate a structural wall (full range, absolute-size threshold) from a local pin (near-money band).
- **The wall-anchored credit-spread strategy is disabled for all instruments** as of 2026-09-01. Its performance figures and its parameters both came from backtests that used the snapshot's own spot field as the entry price, which is unreliable for the reason above.
- The sample period 2026-06-25 to 2026-09-01 was a broad uptrend (GLD +7.4%, SLV +9.7%, USO +26.8%). Nothing here has been validated in a falling market, and a null hypothesis of 50% is wrong for directional signals over such a period — random shorting hits only 39-40%.

## Agent integration

See [`SKILL.md`](SKILL.md) for installation and command-routing guidance. Structured `--json` output is recommended for agent workflows so the model interprets explicit fields rather than scraping prose.

## Project status

undertow is an active research project. Data endpoints and market conventions can change; validate sources and rerun the offline test suite before relying on a new release.
