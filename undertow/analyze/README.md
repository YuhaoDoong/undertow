# `analyze/` — 数据分析层 / Analysis

> Layer 2. Eats `core.models` **only** — fully decoupled from data sources, **pure deterministic computation** (no I/O, no network). The LLM does no arithmetic here; every number comes from this code.
>
> 第 2 层。**只吃** `core.models`，与数据源解耦，**纯确定性计算**（无 I/O、无网络）。LLM 不参与算数，所有数值都出自这里。

## COT positioning / 持仓层

| 文件 | 作用 |
|---|---|
| `positioning.py` | 净头寸 / 周变化分解 / 历史分位 / z-score / 大户集中度。<br>Net positioning, weekly-change decomposition, percentiles, concentration. |
| `signals.py` | 把 `PositioningAnalysis` 翻译成规则化带方向提示：拥挤反指、聪明钱背离、互换商压力（阈值集中在文件顶部，回测后可调）。<br>Rule-based COT signals; thresholds tunable at file top. |

## Options microstructure / 期权微观结构

| 文件 | 作用 |
|---|---|
| `gamma.py` | OI 墙（阻力/支撑/pin）· Put-Call 比 · 做市商 GEX 正负 · 零伽马翻转位。<br>OI walls, P/C ratio, dealer GEX, zero-gamma flip. |
| `blackscholes.py` | 最小 BS 工具：gamma 重定价（求零伽马位）+ 理论定价。<br>Minimal Black-Scholes helpers. |
| `flow.py` | 资金流：单快照异动 + 两日 ΔOI/ΔIV 买卖方判定（Delta 修正相对 IV + 绝对 IV 闸门）+ 多腿价差识别 + 波动率面。<br>Buyer/seller flow from ΔOI×IV-direction, spread detection, vol surface. |
| `expiry_ladder.py` | **近周到期阶梯**：把 60 天混合的墙/资金流拆回单个到期日（未来 3 周五 + 最近月度 OPEX），逐到期复用 gamma+flow，定到期做价差用。<br>Per-expiry slices (next 3 Fridays + nearest monthly) for expiry-specific spreads. |

## Macro & volatility / 宏观与波动率

| 文件 | 作用 |
|---|---|
| `macro.py` | 宏观背景：实际利率/美元/通胀预期 + 波动率指数 → 金银基本面驱动。<br>Real rates / USD / breakeven + vol index. |
| `volregime.py` | 波动率环境：期权偏贵/偏便宜 → 波段级买方/卖方倾向。<br>Rich/cheap vol regime. |
| `vrp_history.py` | 波动率溢价 VRP 跨周期稳定性检验（"这个卖方 edge 能否穿越牛熊"，只落盘存档、不进日报）。<br>Cross-regime VRP stability check (archived, not in daily report). |

## Backtest & aggregation / 回测与综合

| 文件 | 作用 |
|---|---|
| `backtest.py` | COT 信号事件研究：无前视、发布滞后、对齐收益、分位分桶（校准上面的阈值）。<br>Look-ahead-free COT event study. |
| `outlook.py` | **综合研判**：COT/Gamma/Flow + 宏观按【回测校准可信度】加权投票 → 方向+分数+可信度；**近端(墙/流) vs 中期(COT/宏观)双周期分层**；关键位；情景。<br>Weighted multi-factor vote with near/mid horizon split. |

## Strategy modules / 策略层

> 统筹 + 独立子模块：加新策略按此模式加一个子模块，`strategy_hub` 调度。
> Hub + independent sub-modules; add a strategy = add a sub-module, the hub schedules it.

| 文件 | 作用 |
|---|---|
| `strategy_hub.py` | 策略统筹：把多个子模块输出汇成一张"策略总纲"。<br>Assembles sub-module outputs into one overview. |
| `strategy.py` | 方向性情景参数化（期货）：方向随研判、位点随结构、缓冲随 ATR、实时层否决票。<br>Directional futures scenarios. |
| `credit_spread.py` | 方向性信用价差：偏空→熊市看涨价差 / 偏多→牛市看跌价差（跟近端 bias）。<br>Directional credit spreads. |
| `condor.py` | 铁鹰：区间震荡 + 偏卖方环境的规则化结构映射。<br>Iron condor for range/seller regimes. |

## Trade planning / 交易计划（盈亏比 + 斐波，波段交易纪律落地）

> 把方向研判翻译成"**能不能下手**"：先定结构锚，再算盈亏比闸门。纯确定性，LLM 不碰算术。
> From a directional read to an actionable gate: structure anchors first, then a risk-reward gate.

| 文件 | 作用 |
|---|---|
| `fibonacci.py` | zigzag 定位【当前摆动腿】→ 0.382/0.5/0.618 黄金回撤 + 1.272/1.618 扩展目标；传 ratio 补 ETF 行权价锚。<br>Zigzag swing → Fibonacci retracements/extensions (+ ETF strike anchor). |
| `risk_reward.py` | 盈亏比闸门：对每个方向算「现价追」vs「等回调(0.5)」两情景的 R:R 并评级（差/中/优），入场锚斐波、止损锚起涨点、目标取结构墙位（退回扩展位），落地"先看盈亏比、别追、等回调"。<br>R:R gate grading chase-vs-pullback setups; fib entry, structural stop/target. |
| `verdict.py` | **当日决策研判**：规则化合成 近中分层＋资金流＋强信号＋盈亏比闸门 → 做空?/现价追?/短线/长线 四问。逆势微腿识别为回调买/反抽卖（不误报顺腿追）。全程确定性、无 LLM、数字来自上游，可跑无人值守定时任务；交互时 LLM 读它再叠流畅叙述。<br>Rule-based daily decision synthesis (short? chase? swing? core?); deterministic, LLM-free. |

## Live account review / 实盘持仓复盘

> 把研判从"标的怎么看"接到"手上的仓怎么看"。纯确定性，只读，敏感数据不进公开仓库。
> From an instrument read to a review of your actual positions. Deterministic, read-only.

| 文件 | 作用 |
|---|---|
| `portfolio.py` | **实盘持仓理论评价**：解析长桥期权代码 → 按(品种,到期)识别**组合期权**（垂直/跨式/铁鹰/日历/对角/风险反转）→ 逐笔对研判语境复盘（顺逆、行权 vs Gamma 墙、被指派风险、净 Delta、浮盈亏）+ **整品种策略姿态** + **资金约束**（够不够接货）。有实时期权价则用真实市价估值，否则 BS。<br>Combo-aware review of live positions; deterministic, read-only. |
| `healthcheck.py` | **持仓/拟开仓体检**：确定性规则分级预警（高/中/低）——近到期×资金不够接货、卖方盈亏比过低（折算所需胜率）、窄价差+近到期 gamma、裸卖未封顶、逆势、单品种集中度。<br>Rule-based position health checks. |
| `newsfeed.py` | **事件感知**：品种相关新闻 + 影响本品种的临近关键事件（复用 `core.calendar`），高影响事件≤3天置顶告警。只作背景/催化剂旁证，不改判方向。<br>News + upcoming-event awareness; background only. |

## Boundary / 边界

imports `core`（+ 少量分析层内部互引，如 `outlook` 吃 gamma/flow 结果；`portfolio` 吃 blackscholes）；**不 import** `collect`/`report`（`portfolio` 只吃调用方喂入的 `InstrumentContext`，账户数据由 CLI 层从 `collect/longbridge_account` 取后注入）。输出只作**波段级风险情境**，非交易指令、非投资建议。
