**English** · [中文](README.zh-CN.md)

# undertow — reading the institutional-positioning undercurrents beneath the price surface

An institutional-positioning intelligence tool that reconstructs the big-money structure behind **gold / silver / WTI crude / the US Dollar Index**. **Pure standard-library Python, zero dependencies**; can be installed as an **Agent Skill** into Claude Code (`.claude/skills/`) or Codex (`.codex/skills/`) — see `SKILL.md`. Three intelligence layers + backtest + integrated assessment:

1. **COT positioning layer** (CFTC weekly report): the long/short structure and week-over-week changes of managed money / smart money / swap dealers, quantifying "crowdedness, short covering vs. active building, smart-money divergence, swap-dealer directional pressure."
2. **Options Gamma layer** (CBOE delayed data): OI walls by strike (pin / magnet levels), Put/Call ratio, dealer GEX sign (positive/negative), the zero-gamma flip level — i.e. the "key levels" that are the most valuable part of the source article.
3. **Options flow / unusual-activity layer** (self-persisted snapshots): a replication of the author's live 6/24 setup — **large near-expiry unusual activity**. ① A single snapshot finds "today's unusually active" strikes by `volume/OI` (no history needed — usable same day); ② diffing two days' snapshots yields **ΔOI / ΔIV**, classifying bearish/bullish new building vs. unwinding, overlaid on the static walls.

These three layers + the COT-signal backtest all feed into a final **integrated-assessment layer (report)**: it weight-votes a directional lean from each factor by its **backtest-calibrated confidence**, aggregates the **key levels** (walls / zero-gamma / flow-active prices, converted to the commodity price), gives a **rule-based scenario + invalidation level**, and pairs it with **three hand-drawn SVG charts** (price + key levels / OI walls / positioning history), producing a **self-contained HTML report** (viewable directly in a browser).

> ⚠️ **Positioning**: this is a **swing-level risk-scenario tool, not a price oracle**.
> COT lags about 3 days and is unsuitable for intraday; extreme managed-money positioning is often a **contrarian** signal; swap-dealer direction carries OTC-hedging ambiguity.
> The options layer uses **ETF option proxies** (see below), so level conversion to the commodity is only approximate; the GEX sign depends on dealer-positioning assumptions.
> The flow layer has no tick-by-tick trades, so direction is a **heuristic inference**; ΔOI/ΔIV must be accumulated yourself by running `snapshot` daily (CBOE keeps no options history).
> Always decide only after multi-factor confluence with price action.

## Quick start

No third-party libraries required (pure standard library). Run in this directory:

```bash
python3 -m undertow list                    # list configured instruments (gold/silver/oil/dollar, and each one's data layers)
# —— COT positioning layer ——
python3 -m undertow analyze                 # all instruments
python3 -m undertow analyze gold silver     # specific instruments
python3 -m undertow analyze dxy             # US Dollar Index (uses the Legacy report: non-commercial/commercial)
python3 -m undertow analyze --lookback 104  # custom historical lookback in weeks
python3 -m undertow analyze gold --json     # structured JSON (feed to an upper layer / LLM)
# —— Options Gamma layer ——
python3 -m undertow gamma                   # Gamma/OI structure for all instruments
python3 -m undertow gamma gold --json       # structured JSON
python3 -m undertow gamma --horizon 30      # near-month window in days (default 45)
# —— Options flow / unusual-activity layer ——
python3 -m undertow snapshot                # persist today's options chain (run once a day to build history)
python3 -m undertow flow                    # single-snapshot unusual activity + (after ≥2 days) day-over-day ΔOI/ΔIV
python3 -m undertow flow gold --json        # structured JSON
python3 -m undertow flow --no-snapshot      # use only already-persisted data, don't auto-pull today

python3 -m undertow expiry                   # near-week expiry ladder: next 3 Fridays + monthly, each with its own walls + buy/sell side
python3 -m undertow expiry silver            # for setting up expiry-dated spreads (e.g. this Friday's SLV bear call spread)
# —— Signal backtest layer (threshold calibration) ——
python3 -m undertow backtest                # COT-signal historical forward returns
python3 -m undertow backtest gold --json    # structured JSON
python3 -m undertow backtest --horizons 5 10 20  # forward trading days
# —— Event-radar layer ——
python3 -m undertow calendar                 # key-event countdown + this week's live forecast/previous/impact (FairEconomy feed)
python3 -m undertow calendar silver --within 40  # limit instrument + widen the window in days (also auto-embedded at the top of report)
python3 -m undertow calendar --no-live        # don't pull the live feed, use only the manually maintained anchors in config/calendar.json
# —— Integrated-assessment report (four-layer aggregation + visualization + scenarios) ——
python3 -m undertow report                  # per-instrument HTML assessment report (includes 3 SVG charts)
python3 -m undertow report gold             # specific instrument
python3 -m undertow report --json           # outlook as structured JSON (feed to an upper layer / LLM)
# Output goes to data/reports/{instrument}_{date}.html, open in a browser (macOS: open data/reports/...)
```

## Architecture (four layers + a shared core, strict one-way dependencies, modular like a skill)

```
SKILL.md / AGENTS.md        Agent Skill manifest (shared by Claude Code / Codex)
config/instruments.json     instrument registry: add an instrument = edit this JSON, not the code
config/calendar.json        key-event table: FOMC/data/COT/options expiry (US Eastern calendar, hand-maintained, dates must be source-checked)
undertow/
  __main__.py               python -m undertow entry point
  cli.py                    command orchestration (analyze/gamma/snapshot/flow/expiry/vol/backtest/report/calendar/list)
  (each subpackage core/collect/analyze/report ships its own README.md describing that layer's role + file list)
  core/                     【shared core】depends on no other layer, freely referenceable
    models.py               data models (CotReport / OptionsSnapshot etc., pure data)
    config.py               reads config/instruments.json, paths
    clock.py                ★ US-Eastern clock (user is in SGT; trading days are reckoned in US Eastern)
    calendar.py             ★ event calendar: reads calendar.json + countdown/window filtering (event radar)
  collect/                  【data-collection layer】each API converged into semantic models + snapshot repo + cache
    base.py                 data-source abstraction + standard-library HTTP utilities
    cftc_cot.py             ★ CFTC COT: Disaggregated (physical commodities) + Legacy (financials such as the dollar index)
    cboe_options.py         ★ CBOE options (OCC parsing, OI/gamma/iv) + raw persistence + content-fingerprint dedup
    cboe_history.py         ★ CBOE historical daily bars (for backtesting, no key)
    cboe_vol.py             ★ CBOE volatility indices (GVZ/OVX/VXSLV)
    yahoo_futures.py        ★ real futures prices GC=F/SI=F/CL=F/DX=F (direct urllib, zero dependency)
    fred_macro.py           ★ FRED macro (real rate / dollar / inflation expectations, no key)
    faireconomy_cal.py      ★ economic-calendar live feed (FairEconomy public JSON, with forecast/previous/impact)
    store.py                ★ snapshot repo: options-chain raw payload persisted per day as gzip (permanent archive, in git)
    cache.py                file cache (with TTL, temporary, overwritable)
  analyze/                  【data-analysis layer】consumes only core.models, decoupled from data sources, pure deterministic computation
    positioning.py          ★ net position / weekly-change decomposition / historical percentile / z-score
    signals.py              ★ COT rule interpretation: crowding, divergence, swap-dealer pressure (thresholds centralized and tunable)
    blackscholes.py         BS gamma (zero-gamma flip repricing)
    gamma.py                ★ OI walls / Put-Call ratio / dealer GEX / zero-gamma flip
    flow.py                 ★ flow: single-snapshot unusual activity + two-day ΔOI/ΔIV buy/sell side + multi-leg spread detection + vol surface (ATM IV/skew)
    expiry_ladder.py        ★ near-week expiry ladder: next 3 Fridays + nearest monthly, each with its own walls + buy/sell side (for expiry-dated spreads)
    macro.py                ★ macro backdrop: real rate / dollar / inflation expectations + volatility indices
    volregime.py            vol regime: options rich/cheap → swing-level buyer/seller lean
    vrp_history.py          volatility-risk-premium cross-regime check (through bull & bear; archived only, not in the daily report)
    backtest.py             ★ signal event study: no look-ahead, publication lag, aligned returns, percentile bucketing
    outlook.py              ★ integrated assessment: multi-factor weight-voted by confidence + near/mid dual-horizon split + key levels + scenarios
    strategy_hub.py         ★ strategy hub: assembles the independent strategy sub-modules into one overview
    strategy.py             ★ directional scenario parameterization (futures): direction follows the assessment, levels follow structure, buffers follow ATR, live-layer veto votes
    credit_spread.py        directional credit-spread sub-module (bearish → bear call spread / bullish → bull put spread)
    condor.py               iron-condor sub-module (range-bound + seller-favored regime)
  report/                   【report layer】consumes only analyze results, handles presentation only (per-file detail in report/README.md)
    markdown.py             terminal Markdown report (COT / Gamma / flow / expiry ladder / backtest)
    html.py                 ★ self-contained HTML assessment report (embedded SVG, viewable directly in a browser)
    viz.py                  ★ hand-drawn SVG charts (price + key levels / OI walls / positioning history, zero dependency)
tests/                      unit tests (network-free, 15 files / 88 cases)
data/snapshots/             ★ daily options-chain snapshots (gzip, in git = backup; not regenerable)
data/reports/               integrated-assessment HTML reports (by instrument/date, in git; regenerating same-day auto-archives as _rHHMM)
data/cache/                 cache persistence (auto-generated, .gitignore)
```

Clear responsibilities, one-way dependencies: **collect** converges every vendor API into semantic models; **analyze** consumes only the models and does deterministic computation; **report** handles presentation only; **core** is the shared foundation for all three. Any layer can be swapped/tested in isolation. **Add an instrument** = edit JSON; **add a data source** = add one file under `collect/` (a real CME futures-options data source plugs in here just as easily, with zero change to the analysis layer). The LLM does no arithmetic — it only orchestrates and interprets.

## Data sources (with gotcha notes)

| Data | Source | Cost | Lag |
|---|---|---|---|
| COT positioning | CFTC publicreporting.cftc.gov (Socrata `72hh-3qpy` Disaggregated weekly) | free/official | published Friday, as of that Tuesday |
| Options OI / Gamma | CBOE `cdn.cboe.com/api/global/delayed_quotes/options` (**ETF proxy** GLD/SLV/USO) | free/legal | delayed, intraday updates |
| Historical daily bars (backtest) | CBOE `cdn.cboe.com/api/global/delayed_quotes/charts/historical` (GLD/SLV/USO) | free/legal | end of day |
| **Real futures prices** | Yahoo `query1.finance.yahoo.com/v8/finance/chart` (**GC=F/SI=F/CL=F**, COMEX/NYMEX futures) | free/legal | near-real-time |
| **Macro backdrop** | FRED `fredgraph.csv` (**DFII10** real rate / **DTWEXBGS** dollar / **T10YIE** inflation expectations) | free/official/no key | daily T+1 |

> ⏱ **The clock is anchored to US Eastern** (`clock.py`): we watch the US market, so trading days are reckoned in America/New_York.
> The user is in Singapore (SGT), where the local date runs roughly half a day to a day ahead of US Eastern, so the "today" of every snapshot/report is uniformly anchored to US Eastern to avoid misalignment with the real trading day.

> Tested on this machine: CME (403, blocks scraping, not circumvented) is unavailable; **the Yahoo chart endpoint is reachable** (direct urllib, the same layer yfinance sits on, zero dependency); the three hosts CFTC + CBOE + Yahoo are all stably reachable.
>
> **Final levels land on the real commodity price**: the options chain is the ETF's (levels are natively in ETF terms), so the report uses the **live intraday ratio** = real futures price / ETF price to convert all walls / zero-gamma to real gold/silver/oil prices — **avoiding static-multiplier drift** (the measured real ratio for gold ≈ 10.97, not the old 10.8); the price chart also plots GC=F/SI=F/CL=F directly. **Note on WTI**: USO and WTI are on different price scales (USO ≈ 109 vs WTI ≈ 70) and the relationship is nonlinear, so the real oil price is shown with CL=F, but converting USO option levels to WTI is only a same-day approximation with large drift.

**Why options use an ETF proxy rather than the raw COMEX table**:
- CME **hard-blocks** scripted access (403 + an explicit citation of its Data Terms of Use prohibiting automated scraping); we do not circumvent it.
- Yahoo's options endpoint now uses crumb authentication and rate-limits this machine — unstable.
- CBOE delayed quotes are a **legal, public** endpoint, and **every strike comes with gamma/delta/iv directly**, sparing us a home-built pricer.
- GLD/SLV/USO are the industry-standard commodity-option **proxies**: legal and scriptable. The price is that they are **not** the COMEX raw table the article reads — levels are in ETF terms, and ×multiplier ≈ commodity price is only approximate (**USO vs WTI is nonlinear, the multiplier is invalid, only qualitative**).
- For the COMEX raw table you'd need a paid source (Barchart / CME DataMine) or a manual QuikStrike login export; at that point you only add one `datasources/*.py` and **the analysis layer stays unchanged**. The multiplier is calibratable in `config/instruments.json`.

Contract codes (verified): gold `088691`, silver `084691`, WTI crude `067651`.

## Signal / indicator notes

**COT-layer rules** (thresholds centralized at the top of `analysis/signals.py`, tunable after backtesting):
- **MM_CROWDED_LONG / SHORT**: managed-money net position at a historical-percentile extreme → crowded, read as a **contrarian** call for a pullback / short squeeze.
- **MM_FLOW_QUALITY**: decomposition of this week's net change by source — active building (strong) vs. short covering / long liquidation (weak).
- **SMART_DIVERGE_***: smart money (Other Reportables) diverging from managed money (defensive / accumulating).
- **SWAP_DIR_***: swap-dealer directional pressure (replicating the article's logic, strongly flagged for OTC-hedging ambiguity, used only as a supplement).
- **Large-holder concentration** (`ConcentrationStats`, the author's R10 measure): CFTC Concentration Ratios' top-4/8 net-long/net-short concentration (as % of OI) + weekly change + historical percentile — rising net-short concentration = short firepower concentrating into large holders (gold on 6/30 measured 52.8%, +1.7pp, 89th percentile over 156 weeks). Shown in the analyze terminal and in the report's "positioning structure" card.

**Gamma-layer indicators** (`analysis/gamma.py`):
- **OI walls**: within ±15% of spot, the largest call OI = resistance wall, the largest put OI = support wall → pin / magnet candidates. **Depends on no assumptions, the most reliable.**
- **Put/Call OI ratio**: sentiment / skew.
- **Net GEX**: the sign of dealer gamma exposure. Negative gamma = accelerates moves both ways (amplifies volatility); positive gamma = suppresses volatility / prone to pinning. **Depends on the industry-standard but uncertain assumption of "dealers net-long calls, net-short puts."**
- **Zero-gamma flip level**: found by scanning a BS-gamma repricing across different spot prices; once price crosses it, the dealer hedging direction reverses.

**Flow / unusual-activity layer** (`analysis/flow.py`, replicating the author's per-strike "buy vs. sell side" reading):
- **Today's unusual activity** (single snapshot suffices): near-month strikes near spot with high `volume/OI`. **Volume ≫ OI = mostly new positions opened today**, the **same-day precursor** to a ΔOI move.
- **Day-over-day buy/sell determination** (needs ≥2 days of snapshots): per (expiry, strike, C/P) it gives **ΔOI / current OI / precise Delta / Delta-corrected ΔIV / verdict**, replicating the author's table.
  - **The key trick**: delayed data has no tick-by-tick trades (no "who was aggressive" tick tag), so buy/sell side is **inferred, not measured** — using the **direction of the IV change** as a proxy. The intuition: IV is the option's cheapness/richness, and **whoever is in a hurry pays** — at opening, if the premium was bid up rich the buyer was in a hurry; if it was pressed cheap the seller was in a hurry. The full four-quadrant table:

    | ΔOI | ΔIV | Verdict | Logic |
    |---|---|---|---|
    | Up | Up | **Buyer** (protection / chasing) | premium lifted as the new position is built → buyer actively pays up |
    | Up | Down | **Seller** (writing to cap / building support) | premium pressed down as the new position is built → seller is supplying |
    | Down | Up | **Seller retreating** | premium rose on the close → seller pays to cover and exit (put side = weakening support) |
    | Down | Down | **Buyer taking profit** | premium fell on the close → buyer exits for profit / stop (put side = panic ebbing) |

  - **Two denoising steps** (skip them and you get systematic misjudgment): ① **Delta correction**: first subtract `skew slope × Δspot` — once spot moves, the IV at each strike shifts mechanically along the skew curve, unrelated to buying/selling; ② **relativization**: when there are enough strikes (≥8), further subtract the median of the whole chain's corrected ΔIV, removing the "the whole market's vol rose/fell together" translation term. The residual left is the **strike-specific** buy/sell pressure (= the author's "relative IV change" column).
  - **Noise gates**: |ΔOI| < 50 contracts doesn't make the table; corrected-ΔIV absolute value < 0.08pp is judged "noise" with no direction; ≥1.0pp of call selling pressure is upgraded to "extremely strong suppression"; rows without a prior-day IV are characterized by OI direction only ("new build / unwind").
  - Verdict tiers: buyer protection / slight protection, seller building support, seller retreating, buyer taking profit, (call) extreme / seller / slight suppression, buyer, noise — **measured against the author's WTI 6/22 raw table, 19 of 20 rows match** (only 1 row had OI down but the author, given a large IV rise, discretionarily judged it a buyer).
  - **Overlaid on the static walls**: new OI piling on the put wall + IV up = the author's kind of self-fulfilling breakdown warning.
- **The "today's positioning moves" quick-read paragraph** (`structural_moves`): automatically picks the most structural moves out of ΔOI and stitches them into plain-language quick-read, prioritizing **wall rolling** (adjacent same-type strikes, one down one up = a defensive line / target shifting, each leg carrying its own buy/sell verdict, e.g. "put side 3,931 (put wall) unwound, 4,040 buyer protection — **capital is more bearish**") > large adds/subtracts on a wall > the single largest new build; the net-direction conclusion is voted only by legs that carry IV info, neutral legs abstaining. Plus a flow net-lean (buy/sell-side weighted, spread-protection legs already netted out).
- **The "structure vs. yesterday" quick-read sentence** (`gamma.structure_delta`): zero-gamma displacement (including a "closing toward spot" note) + **thickening/thinning** when the wall hasn't moved (same-strike OI diff vs. yesterday) + wall migration; yesterday's structure is recomputed anchored to yesterday's date to avoid time-to-expiry weighting distortion. **Composite-score trend**: each day's bias_score is persisted to `data/history/outlook_scores.json`, and the quick-read direction block automatically gives comparisons like "-4.8 → -3.4 vs. yesterday, bearishness easing."
- **The "counterparty warning" quick-read paragraph** (`counter_signals`, in red): the strongest ΔOI signals **opposite** to the integrated-assessment direction (when bearish, pick the bullish moves, and vice versa) + short veto-vote labels from the strategy module — read the report by looking at the opposing side first; as counter-evidence grows, directional confidence should be lowered. The quick-read is organized into four blocks: [Direction] [Key levels] [Positioning moves] [Counterparty warning].
- ⚠️ Buy/sell side is inferred from the **IV-direction proxy**, not trade aggressiveness; "retreat / take profit" only sees the close, and who moved first is inferred; boundary rows (IV change near the noise line) may differ from a human discretionary call; spread structures can masquerade as directional bets (see multi-leg detection); CBOE keeps no options history, so you **must accumulate it yourself by running `snapshot` daily** (already automated via launchd, see below).

**Near-week expiry ladder** (`analyze/expiry_ladder.py`, the `expiry` command + auto-embedded in report) — serves **expiry-dated short-term spreads**:
- The main report's walls/flow **lump all expiries within 60 days into one bucket** to show "where the overall wall is"; but to set up a spread on **one specific expiry** (e.g. this Friday's SLV bear call spread) you need **that single day's own structure**. This module slices it back out per expiry.
- **Selection**: the next 3 Fridays (this week / next / the one after) + the nearest monthly OPEX (third Friday) — labeled by calendar-week delta, anchored to the real today.
- **Each expiry read independently**: after **filtering the snapshot to that expiry alone**, it reuses `gamma` (call/put OI walls, top-3 walls, PCR) + `flow` (that expiry's own day-over-day ΔOI buy/sell verdict), with the exact same methodology as the main report. Per-expiry reads can differ sharply — measured on one day, two near expiries read "this Friday bullish · call buyers" vs "next Friday bearish · call selling."
- ⚠️ Per-expiry buy/sell needs **two weekday snapshots** (weekend/pre-market duplicates leave OI unchanged, so those rows show "awaiting the next snapshot"; walls always show); the nearer the weekly, the noisier the IV; still ETF-proxy levels, qualitative only.

**Backtest layer** (`analysis/backtest.py`) — calibrates the signals/thresholds above against historical prices:
- No-look-ahead weekly signal recomputation; entry uses the COT Friday-publication lag; forward 5/10/20 trading-day returns.
- **Aligned return** = the return of trading in the signal's direction; to count as effective it must be significantly positive, hit rate > 50%, and beat the "unconditional baseline."
- **MM net-percentile bucketing**: directly checks whether "the more crowded → the lower the forward return" holds, calibrating the crowding threshold.

**Integrated-assessment layer** (`analysis/outlook.py` + `viz.py` + `report_html.py`) — pulls the four layers into one visualized report:
- **Directional lean**: each factor (COT signals / wall room / P-C ratio / flow) is weight-voted by its **backtest-calibrated confidence**, yielding bullish/bearish/neutral/conflicted + composite score + confidence. **Weights and rationale are listed item by item and auditable**; known-unreliable signals like crowding-as-contrarian are down-weighted.
- **Key levels**: walls / zero-gamma / near-expiry pin / flow-active prices, all converted to the commodity price.
- **Scenario reasoning**: rule-based if-then (hold X → range / break Y → trend amplification), **including invalidation levels** — what it gives is "which levels to watch and what falsifies them," **not a point-price prophecy**.
- **Visualization**: 3 hand-drawn SVGs (price + key-level horizontals / OI-wall diverging bars / managed-money net-position history), embedded into the self-contained HTML. **Pure standard library, zero dependency.**
- ⚠️ The "forecast" is a deterministic aggregation of rules, not an oracle; the LLM does no arithmetic. Always decide only after confluence with price action.

**Strategy module** (`analyze/strategy.py`, futures; options a placeholder) — translates the assessment into **executable, checkable scenario parameters**:
- **Direction first**: entirely determined by the integrated-assessment bias (neutral/conflicted → no levels given, monitor only); set the direction, then set the levels.
- **Two scenario types**: `rejection/absorption at a wall` (counter-trend, trigger zone = 1×ATR before the wall) and `zero-gamma flip confirmation` (with-structure, daily close break/hold of the flip point + next-day pullback confirmation); each carries trigger condition / reference zone / invalidation line / target (with a mechanical R:R) / current status.
- **Live-layer veto votes**: the Gamma environment, spot relative to zero-gamma, and the vol surface (skew convergence / buyer confirmation) are each listed as a vote — with ≥2 vetoes the module rules "don't fire, wait for the trigger."
- **ATR scaling**: buffer-zone width and position-size hints both scale to the 14-day Average True Range (degrading to close-to-close range when high/low prices are missing).
- **Take-profit / stop-loss plan** (templated sentences): stop-loss = the invalidation line (with the spread and percentage of per-unit risk); take-profit = scaling out along structural levels (first target trims half, second target exits, each with an R:R); after the first target is reached the stop moves to the entry reference (breakeven).
- **Presented in the report as a "trade-ticket" sub-card**: the ruling banner on top → veto votes as red chips → **structure-timeline chart** (last N days' HLC daily candles vs. day-by-day zero-gamma/wall polylines, gamma recomputed day by day from historical snapshots) → one ticket per scenario (status pill / trigger condition / **price-track chart**: stop-loss red · entry zone blue band · spot black dot · take-profit green / 🎯 take-profit/stop-loss row).
- **Cross-day window state machine** (flip confirmation): uses the last ≤10 **completed daily closes** against the day-by-day zero-gamma sequence to determine — not triggered / break day (T+0, awaiting pullback) / broken · awaiting pullback / **confirmation established · position management** (explicitly noting "by the rules a position should already be on, this is not a fresh entry point") / reset once the break is reclaimed; if the window's first day is already a break it's marked "or earlier" (left-censored). Wall rejection also carries a window note (recently touched the wall and was pushed back).
- ⚠️ The output is a **rule-based framework for a person / advisor to reference, not a trade instruction**; cross-venue execution requires shifting the basis yourself.

**Methodology**: per-strike "buyer vs. seller" reading (OI change × Delta-corrected relative IV direction + absolute IV gate), vertical-spread structure detection, vol-surface "buyer confirmation" checks, the integrated layer's "near-term vs. medium-term" dual-horizon layering, and other rules all live in `undertow/analyze/` (the flow / gamma / outlook and other modules), and each is traced item by item against the daily report's directional conclusion for continuous self-calibration.

### Key backtest findings (2024-06 → 2026-06, indicative)

- **Crowding-as-contrarian is instrument-/regime-specific**: `MM_CROWDED_LONG` on **mean-reverting crude** delivered +5.4% 20-day aligned return, 100% hit rate (n=7), with monotonically declining percentile buckets (the more crowded, the more it falls); but on **one-directionally-bullish gold** it failed entirely (aligned −2.6%, 12% hit rate, non-monotonic percentiles).
  → **In a trending regime, don't use positioning crowding as a contrarian signal**; that signal should get a trend filter.
- `SMART_DIVERGE_BULL` (smart money accumulating counter-trend) is fairly stable on gold/silver (67–75% hit rate).
- `SWAP_DIR_*` and other small-sample (n<10) signals **should not be taken seriously**.
- This is precisely the value of backtesting: it falsifies/confirms "seemingly reasonable" rules and tells you **which signal is credible on which instrument, in which regime**.

## Daily auto-update (launchd, zero LLM involvement)

`scripts/daily_update.sh` is triggered by macOS launchd every day at **14:05 (Singapore / Beijing time)**: snapshot the day's options chain → produce the three-instrument reports only if there's new data → auto commit + push (= GitHub backup).

- **Snapshot time window (hard constraint)**: the script runs only during **ET 1:00–8:59 a.m.** — the lower bound waits for ET to pass midnight (avoiding a name collision with yesterday's snapshot) **and** for OCC to finish its overnight update of option OI (anything grabbed earlier is dirty "today's volume + yesterday's stale OI" data that ruins the ΔOI reading); the upper bound is the US market open at 9:30 ET (after which the chain becomes intraday real-time data, no longer a clean "yesterday's close chain"). Better to skip than to miss the window. Converted to Beijing time: **DST 13:00–21:30 / standard time 14:00–22:30**, and the 14:05 trigger point is safe under both regimes.
- On a market holiday, if the deduped snapshot content has no new file → no report, no commit (avoiding garbage reports).
- Install: `~/Library/LaunchAgents/com.yuhaodoong.undertow.daily.plist` (`launchctl bootstrap gui/$UID <plist>`); log at `~/Library/Logs/undertow-daily.log`.

## Tests

```bash
python3 tests/test_positioning.py
python3 tests/test_gamma.py
python3 tests/test_backtest.py
# or python3 -m pytest tests/ -q
```

## Installing as an Agent Skill

This repository is itself an **Agent Skill** (the root `SKILL.md` provides the manifest, shared by Claude Code and Codex):

```bash
# Claude Code
git clone https://github.com/YuhaoDoong/undertow ~/.claude/skills/undertow
# Codex CLI
git clone https://github.com/YuhaoDoong/undertow ~/.codex/skills/undertow
```

Once installed, asking "how do gold's longs/shorts look now / is the dollar's positioning crowded / where are the crude options walls" triggers it, and the agent runs `python -m undertow <command>` in the repo root and interprets the conclusions for you. Zero dependency, no pip needed.

## Roadmap (possible next steps)

1. ~~COT positioning layer~~ ✅ · ~~Options Gamma layer~~ ✅ · ~~price alignment + backtest~~ ✅ · ~~flow/unusual-activity layer + snapshot persistence~~ ✅ · ~~integrated assessment + visualized HTML report~~ ✅ · ~~layered refactor + Skill packaging~~ ✅ · ~~US Dollar Index (Legacy COT)~~ ✅
2. **Real CME futures options** (the biggest gap): the current options layer is an ETF proxy (USO ≈ WTI is weak). Wire in the **IBKR API** as a new `collect/` data source to get per-strike real OI/IV/greeks, and gamma/flow/spread-detection all get reused — crude's credibility improves directly. Requires a brokerage account + a CME market-data subscription.
3. ~~**Accumulate daily snapshots**: a scheduled job runs it automatically~~ ✅ (launchd every day at 14:05, see the section above)
4. **US single-name stocks**: the architecture already supports instruments with no COT (options + macro only); after wiring in a single-name/ETF options source, adding a config entry suffices.
5. **Tune thresholds per backtest**: change `signals.py` per the backtest conclusions (trend filter, per-instrument thresholds).
6. **DXY options layer**: if a suitable dollar-options source is wired in (e.g. UUP or IBKR), complete gamma/flow + an HTML report for the US Dollar Index.
