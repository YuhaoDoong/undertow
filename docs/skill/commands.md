# 命令参考（全量）

<!-- 由 SKILL.md 拆出（2026-10-15 重组）；内容原文搬迁，未改口径。入口见仓库根 SKILL.md。 -->

## 前置
- Python 3.9+；**无需 pip 安装任何依赖**（仅标准库 urllib/json/statistics…）。
- 所有命令在**仓库根目录**运行：`python -m undertow <command> [品种...]`。
- 品种 key：`gold` `silver` `wti` `dxy` `qqq` `tqqq` `tlt` `spy` `iwm`（`python -m undertow list` 查看各品种已配齐哪些层）。
  按用途分两类：
  · **交易品种** `gold` `silver` `qqq` `tqqq` —— 有实盘或计划仓位
  · **分析品种** `wti` `dxy` `tlt` `spy` —— 不交易，但驱动或映射前者
  · **观察品种** `iwm` —— 信息大半与 SPY 冗余（ρ 0.89），加它是为**提前攒期权链历史**；
    本金约 $800 后转为交易候选（点差仅 5%，远好于 TQQQ 的 20%）
  ⚠️ `qqq`/`tqqq` 的 commodity 层共用 `NQ=F`——**价格类读数可共用（比值量纲抵消），但位点必须各自换算**：
  TQQQ 是 3 倍杠杆，行权价与纳指点位无固定比例。

## 研判与数据命令（原表）
| 命令 | 作用 | 产物 |
|---|---|---|
| `python -m undertow report [品种...]` | 四层聚合综合研判 + **当日决策研判**（做空?/现价追?/短线/长线 四问）+ **近端资金流强信号置顶告警** + SVG 图 + 斐波盈亏比闸门 + 策略情景参数化 | 自包含 HTML 落在 `data/reports/`；多品种自动出 index 综合研报（每品种带决策总纲） |
| `python -m undertow analyze [品种...]` | COT 持仓分析（净头寸/拥挤度/聪明钱背离） | 终端 Markdown；`--json` 给结构化 |
| `python -m undertow gamma [品种...]` | 期权 Gamma/OI 墙/零伽马 | 终端 Markdown |
| `python -m undertow flow [品种...]` | 买卖方资金流 + 多腿价差识别 + 波动率面（ATM IV/25Δ·10Δ skew 日变化 → 期权端是否确认价格） | 终端 Markdown（需≥2 天快照） |
| `python -m undertow expiry [品种...]` | **近周到期阶梯**：把 60 天混合的墙/资金流拆回【单个到期日】——未来 3 个周五 + 最近月度 OPEX 各自独立的 call/put 墙 + 逐 ΔOI 买卖方（定到期做价差用） | 终端 Markdown；也自动嵌入 `report` |
| `python -m undertow fib [品种...]` | **斐波那契回撤 + 盈亏比闸门**：确定性定位当前摆动腿 → 0.382/0.5/0.618 黄金回撤区 + 扩展目标，再算「现价追」vs「等回调」两情景的盈亏比并评级（差/中/优），把「先看盈亏比、别追、等回调」这套交易纪律落成数字（含 ETF 行权价锚，实盘定腿用） | 终端 Markdown；也自动嵌入 `report` |
| `python -m undertow account` | **实盘持仓理论评价（只读）**：读长桥账户当前持仓，逐笔放进 undertow 研判语境复盘——组合期权识别(垂直/铁鹰/日历)、整品种策略姿态、资金约束、**🩺持仓体检**(近到期/盈亏比/gamma/集中度分级预警)。**绝不下单**；持仓属敏感数据，HTML 落 gitignore 的 `data/account/` | 终端 Markdown + 本地私有 HTML。`--no-html` 仅终端 |
| `python -m undertow consult ["问题"]` | **咨询/开仓前问诊（只读）**：把研判＋持仓评价＋体检＋你的问题装成"咨询上下文包"（数字全由确定性引擎算好），供 AI 给意见。`--thesis ID` 带上你的**事前判断**（AI 须先独立判读再逐条对照，并给出 实盘/模拟/否决 三选一结论）；`--pre-trade SPEC` 对拟开仓做开仓前问诊；`--json` 出机器可读完整包（供其它 AI 接入） | 默认打印可投喂任意 LLM 的 prompt；`--json` 出 JSON 包 |
| `python -m undertow serve` | **本地只读 HTTP API**：把咨询上下文包暴露成 localhost 端点（标准库 http.server），方便接入其它 AI。`GET /consult?q=...`、`/prompt`、`/positions`、`/health`；**无任何下单端点** | 本地 HTTP 服务（默认 127.0.0.1:8787） |
| `python -m undertow backtest [品种...]` | COT 信号事件研究回测 | 终端 Markdown |
| `python -m undertow vol [品种...]` | **波动率溢价 VRP 跨周期检验**：「卖方这个 edge 能不能穿越牛熊」。属长周期研究，只落盘存档 `data/history/vrp/`，**不进每日报告** | 终端 Markdown |
| `python -m undertow snapshot [品种...]` | 落盘当日期权链原始全字段 | gzip 存 `data/snapshots/`（纳入 git） |
| `python -m undertow calendar [品种...]` | 事件雷达：关键节点倒计时 + **实时预测/前值/影响**（本周自动拉 FairEconomy 公开 feed，远期用手维护锚点） | 终端；也自动嵌入 `report` 顶部。`--no-live` 仅用本地锚点 |
| `python -m undertow soul [--check]` | **交易灵魂档案（个人专属·只读）**：你的交易体系/铁律/纪律/已知弱点/教训；`--init` 从公开模板 `config/soul.template.json` 生成本地私有档案；`--check` 用其中的机器限额（**双层仓位：止损风险%(软,决定仓位) + 最大亏损%(硬,跳空防线)**/集中度/**闸门分两套：方向性看R:R、卖方看胜率边际**/空头腿了结线/禁强平结构）**核查当前持仓是否破戒**。档案自动进 `consult` 并置于 prompt 最前——任何 AI 先读你的规则再给意见 | 终端 Markdown；档案存 gitignore 的 `data/soul/` |
| `python -m undertow journal [--capture\|--date\|--limit]` | **交易日记（本地私有）**：`--theses` 看**事前判断记录**（开仓前写下方向/依据/失效条件/目标，事后标对错——**判断对错与交易盈亏分开统计**，两者背离即说明问题不在判断力）；`--capture` 从券商**自动抓当日成交**（时间/合约/方向/张数/成交价/金额/手续费）+ 账户前后变化；再补**复盘、盖棺定论、心情**。档案记『规则』，日记记『发生了什么』，互补 | 终端 Markdown；存 gitignore 的 `data/soul/journal.json` |
| `python -m undertow event LABEL [品种...]` | **事件影响捕捉（只读）**：数据/事件落地【前】与【后约10分钟】各捕一次横截面快照（**期货实时价为主口径**——≈23h交易、数据公布瞬间最真实；ETF 常规/夜盘/盘前/盘后**各场次分开记**；+ATM IV+过热分+墙位+新闻），`--compare KEY` 出前后对比表。**事件时点不可再生**，攒够后可用自己的数据统计各类事件对各品种的真实冲击。盘前 IV 不更新会标⚠陈旧 | 快照存 `data/history/events/`（入 git，市场数据非个人） |
| `python -m undertow plan [--check\|--notify\|--orders ID]` | **计划交易（只读监控）**：记录完整交易计划（触发价+出场四要素+张数+闸门）；`--check` 抓实时价核触发/接近；`--notify` 弹通知+落 ALERT（可接 launchd 盯盘）；`--orders ID` 输出**可照抄的下单参数**。**绝不下单/撤单/改单**——执行永远是你在券商端的动作 | 终端 Markdown；计划存 gitignore 的 `data/soul/plans.json` |
| `python -m undertow tech [品种...]` | **技术面：超买超卖（回测校准）+ 趋势结构**。<br>主读数是**两个正交维度**：①偏离度 `(现价−MA20)/ATR14`「离常态多远」；②回撤度 `(现价−60日最高)/ATR14`「从近期高点掉多少」——两者分位相关仅 0.73，是真互补（而 20 日回撤/区间位置与偏离度相关 0.91/0.95，是同一件事换写法，故不采用）。**每个读数强制带上该 (档位×regime) 的回测边缘/跑赢中性桶比例/触发次数/检验样本量/Welch t**，不输出未校准的裸标签。<br>⚠️ 实测边界：**只有超卖侧四格可用**（|t|≥2 且检验样本≥50）；**超买侧一格都不可用**，只能读作「追高性价比差」，**不可读作「要反转」**；**仅日线成立，1H/4H 是噪音**。<br>传统过热分（RSI/KDJ/CCI/布林/BIAS）保留但**已降级**——五个分量彼此相关 0.79~0.93，是同一信息数了四遍。两者分歧时以校准读数为准 | 终端 Markdown；也进 `report` 卡片、`consult` 包与 InstrumentContext |
| `python -m undertow live` | **持仓实时体检（只读）**：长桥 Level-2 实时盘口 → **真实可平仓价**（多头腿按 bid 卖、空头腿按 ask 买回）。⚠️ 券商 App 的持仓盈亏用 last 价，对流动性差的腿会**系统性高估**（实测同一时刻 App $100 / 真实可平仓 $67，差 $33）——**止损判定必须用本命令的数**。另出**生命周期台账**：只吃现金流水，不信券商成本价（后者在部分减仓后会被改写成该轮打平价）。任一持仓拿不到盘口时，总敞口显式标为「不完整的下界」 | 终端 Markdown；无持仓则直接返回 |
| `python -m undertow backtest-stretch [--mode\|--compare\|--emit]` | **重跑超买超卖校准表**：长历史（1993→2026）+ regime 分层 + 不重叠 Welch 双样本 t。`--compare` 对照 combo/stretch/drawdown 三种口径；`--emit` 输出可粘回 `stretch.py` 的 CALIB 字面量。改了指标参数就重跑，**别手改数字** | 终端 Markdown |
| `python -m undertow signals [品种...] [--rebuild\|--backfill\|--horizon N]` | **强信号台账**：把每次候选（含被逆向价格闸门拦下的）的全部分量落盘，日后回填真实走势。`--rebuild` 用已落盘快照回放重建；`--backfill` 用真实价格回填前瞻收益/漂移/牛熊。**这层是全报告唯一顶红色 ⚡ 告警却从未回测过的部分**——核心三闸门需历史逐行 OI，免费源拿不到，只能向前累积（8 品种约 0.7 个/交易日，n=100 需约 7 个月） | 终端 Markdown |
| `python -m undertow shadow capture\|quote\|settle\|report` | **前瞻配对影子账（W05，只读，从不下单）**：检验「按墙选卖腿」是否比「不看 OI、按 ATR 距离选」多赚。每天盘前冻结每品种每侧的 A（局部最大墙）/ B1（1.0×ATR）/ B2（2.0×ATR）/ B3（卖腿 |Δ|≈0.20）四组价差（无候选也记原因，另记期限结构是否倒挂）；盘中两个窗口（ET 10:00–10:20；收盘窗 = 核心收市前 30~15 分钟，正常 15:30–15:45、13:00 收市日 12:30–12:45，由预存 NYSE 日历给出）只追加两腿原始盘口尝试（失败可在窗口内重试，取第一次合格报价，不挑最好）；入场、持仓标记、1×/2× 权利金止损全部在结算时从原始观测派生，缺标记记为「路径未知」而不是「未触发」；所有臂同美元宽度（2×众数行权价间隔）。各终点独立成熟后结算：**主终点「到期前最后交易日收盘窗的退出政策」**（两腿报价合格且长腿有买价 → 整体平仓点值；长腿报价有效但买价为 0 → 只买回短腿、长腿持有至到期：到期虚值现金流 0 为点值，到期实值处置未知、只有上界；残值按 0 另列为情景），副终点含报价入场+到期内在值、收盘越线次日开盘平仓、两种止损、快照模型；三种突破（窗末/期间收盘/盘中）分开记。`report` 按 ETF / 杠杆 ETF / 个股三池分开出 A 全体、A 配对、B 配对三个均值与 A−B 配对差（区间值按 [A下界−B上界, A上界−B下界] 传播，有残腿处置未知即判未决；另列「仅双腿整体退出」条件样本与「残腿按 0」情景；沿交易日历的循环移动块 bootstrap，主 5 日块、敏感性 10 日块，另附不重叠入场敏感性；日期不足 4 个完整块报样本不足，常数样本报退化）；2026-12-31 正式检验日之前一切判定都标「探索」。报告末尾列机会分母（池×品种×侧×交易日，每格落入未生成/无候选/入场缺/未成熟/未知/仅A有值/可配对之一）、逐品种可配对率与池内权重、缺失是否集中在大波动日、各规则描述性指标（净$、损益/宽度、损益/风险、收权金/宽度、费用/收权金）；12/31 之后首次正式运行的结果冻结在 formal/，之后差异只追加修订。`windows` 打印当天窗口供调度脚本调用。当前配置 shadow-v5-20260926。只有「已认证 session + 开盘前记录」计入主样本；`--as-of` 回放写 replay/ 不进主样本。**50 笔只是数据复核节点，不是放行线**。已接入每日任务 | `data/history/shadow/`（入 git） |
| `python -m undertow shadow freshness-audit` | **v5 输入新鲜度侧车（只读 v5，只追加；Codex 056）**：v5 盘前取的基准收盘常是 T−2（CBOE 日线没更新 T−1；截至 10/16 219 行中 197 行）。逐行记 expected（预存日历 T−1）/ 实际基准日 / 落后交易日数 / 受影响字段；v5 冻结行、配置、收益都不改。`shadow report` 顶部据此披露「本账检验的是含该输入行为的实现」。已接入每日任务 | `data/history/shadow/shadow-v5-20260926/sidecar/`（入 git） |
| `python -m undertow mainwall capture\|quote --window open\|close\|sample\|settle\|report\|audit\|coverage` | **主墙近侧卖方价差前瞻研究账（只读，从不下单；不是实盘放行；预登记 `docs/prereg/2026-10-17_mainwall_v0.1.md`）**：用户「离哪个墙近就卖哪边墙」。盘前用新鲜 T−1 输入（CBOE → Yahoo，不拼接、源间核对）冻结金银两侧「排名主墙」（前收虚值侧 25% 内、到期 ≥ E 的 OI 最大档，连续记第二名/份额/期限分解）；开盘窗先冻结 Sdec 再选近侧，比较 A（卖主墙）与 B（同侧 1·ATR），退出沿用 v5 主终点；远侧与铁鹰只作次要描述；盘中采样记标的到两侧墙的距离。失败输入只追加尝试、不占当天冻结键；开盘后不再冻结。12/31 只是独立阶段检查。`coverage` 每次唤醒重算已到截止的应有项，缺口按稳定 id 登记（首次发现事实冻结、当前状态可演进），rc 0 无缺口 / 1 有缺口 / 2 核查本身出错；调度层只认身份核验过的状态文件。已接入 daily 与 session_hooks ⑰ | `data/history/mainwall/`（入 git） |
| `python -m undertow shadow chain` | **开盘后近价全链快照（只读）**：每个交易日 ET 10:15–10:35 抓 15 个品种的【有限近价链】（≤14 天到期、按当时现价 ±10%；更远的保护腿/到期不在内），保存 bid/ask/挂单量/IV/希腊值/成交。报价时刻按标的最后成交时间（CBOE 延迟约 15 分钟）认证：落在 ET 10:00–10:20 才算开盘窗报价，否则只作研究用途。结构错、空链、全不匹配分别记录，不当成功；当日已有文件严格读回校验，坏文件隔离后重抓。已接入 session_hooks（⑥），约 0.8MB/天 | `data/snapshots/options_open/`（入 git） |
| `python -m undertow shadow fieldcheck --phase pre\|open\|close` | **每日现场核验（只读；用户 2026-09-28「自动触发，不要等我提醒」）**：session_hooks ⑧ 在交易日自动跑三次 —— pre（ET 09:00–09:29）：主池各品种当日机会行、两份来源抓取时刻、台账方向指纹、方向准入结论；open（ET 10:40 起）：开盘窗各腿入场报价、近价全链快照内容、已结束的盘中采样桶；close（ET 16:20 起）：收盘窗、全天采样桶、待发布记录中「已发布却未清理」的条目。未到时点记「待定」不算异常；有异常弹通知一次。台账损坏也作为异常报告，不让核验崩掉 | `data/history/fieldcheck/`（入 git） |
| `python -m undertow shadow sample` | **盘中时段采样（只读；用户 2026-09-27）**：ET [09:45, 13:00) 每 15 分钟一个桶，共 13 个，桶名只是标签，实际时刻看 started_at/ended_at；13:00 收市日截到 [09:45, 12:45)。对当天入场与尚未到期的候选腿及标的各记一次买价/卖价/挂单量，回答「在这个时段开仓实际能收多少」。逐合约区分成功观测与取数失败：一侧无挂单算观测；取数失败的在本桶内下次唤醒重取，失败尝试全部保留，研究时每个合约取本桶第一次成功观测，不择优。状态分 complete/partial/failed/unchanged，失败按当前桶当场提醒；`--check [日期]` 收尾核对各桶，每日任务次日盘前自动跑。与 v5 预登记无关：不写 v5 账本、不改 10:00–10:20 入场窗与主终点。已接入 session_hooks（⑦） | `data/history/shadow_samples/`（入 git） |
| `python -m undertow shadow bars [--since D] [--all]` | **候选价差的历史盘中成交价（只读；用户 2026-09-27）**：长桥 1 分钟 K 线补 v5 前瞻行与回放行候选腿的逐分钟成交价（只有成交价、无买卖价，是补充数据，不替代 v5 保守入场价）。已到期较久的合约长桥会查不到（该接口当时的返回，不等于永久不可得；`--retry-missing` 可重查）→ 先补最早的；长桥按不同代码数限额（实测 400，重置周期未查证）→ 默认只补主池 A、B1，配额用尽 rc=3、每日只提醒一次。已接入每日任务 | `data/history/option_bars/`（入 git） |
| `python -m undertow shadow exec [--session D]` | **账户风险预算账（S05，只读，从不下单）**：研究账有候选 ≠ 本账户可承受一组。对当日每个候选按 `analyze/risk_policy.py`（AGENTS.md 双层限额的唯一来源）给出理论风险预算 pass/fail/unknown 与组数上限；额度只扣本账户真实持仓，此前候选的假设占用只列出不扣。券商执行性（垂直价差实际保证金、单腿退出、到期处置）未核实；各候选互为备选，组数不可相加 | `data/account/shadow_exec/`（gitignore，含账户金额） |
| `python -m undertow news [品种...]` | **事件感知（只读）**：品种相关新闻（长桥）+ 影响本品种的临近关键事件；**高影响事件临近（≤3天）置顶告警**（如"到期日撞 Core PCE"）。新闻只作背景/催化剂旁证，不改判方向 | 终端 Markdown；也进 `consult` 上下文包 |
| `python -m undertow list` | 列出品种与各自数据层 | 终端 |

留空品种 = 全部。全局开关：`--no-cache`（绕过缓存）置于子命令前，如 `python -m undertow --no-cache analyze gold`。

## 架构（四层 + 公共核心，改一层不动其它）
```
undertow/
  core/     models / config / clock          公共核心
  collect/  各数据源 + 快照仓库 + 缓存          数据收集层
  analyze/  positioning/gamma/flow/macro/      数据分析层（只吃 core.models）
            outlook/strategy/backtest/signals/blackscholes
  report/   markdown / html / viz             报告层
  cli.py + __main__.py
```
- **新增品种** = 改 `config/instruments.json`，不动代码。
- **新增数据源** = `collect/` 加一个文件（真·CME 期货期权数据源也在此即插即用，分析层零改动）。

## 原表未列的命令（2026-10-15 补齐）

| 命令 | 作用 | 产物 |
|---|---|---|
| `python -m undertow quote [SYM...]` | 实时报价（常规/盘前/盘后/夜盘分场次）——**查价一律走这里**；事前判断开头先跑它 | 终端 |
| `python -m undertow walls [品种...] [--layer near\|mid\|both] [--commodity]` | 期权墙位：最近几道墙 + ΔOI + 买卖方向（**查墙位一律走这里**） | 终端 |
| `python -m undertow chain <品种>` | 现价附近各行权价参考价（选腿用） | 终端 |
| `python -m undertow cal-spread` | 日历/对角价差核算（按需，不进研报） | 终端 |
| `python -m undertow ta` | 技术面备用层：多周期数据体检 + MACD（不进研报） | 终端 |
| `python -m undertow report-v2` | 研报 v2：只放已证实内容（目前为期权墙总览） | HTML |
| `python -m undertow claims` | 指标主张权限清单（证据等级、允许用途；Codex 015） | 终端 |
| `python -m undertow archive-inputs` | 研报输入存档（价格/波动率/FRED/COT 尾部与月度全量） | `data/history/inputs/` |
| `python -m undertow dirledger record\|score\|conviction-record\|author-add` | 方向判断台账：盘前冻结的偏度读数、外部作者判断（私有）、价格计分 | `data/history/` 与私有 `data/soul/` |
| `python -m undertow trial status\|init\|fund\|trade\|verify-trades\|reconcile\|resume\|r\|r-anchor` | 实盘试行（政策 B）：资本、额度、暂停 latch；reconcile 只出候选，正式登记走人工 trade/verify-trades；`r` 预算权益 R 快照、`r-anchor` 采日初基准（收盘后；盘中只能 `--bootstrap --note`） | 私有 `data/account/` |
| `python -m undertow spread-quote <标的> <到期> C\|P <K1/K2> [--credit] [--qty N] [--snapshot]` | 组合单整单定价：逐腿买一/卖一 → 净价三档、建议挂单区间（系数未校准）、含费经济量（`spread_econ` 唯一来源）、政策 B 与收益风险比下限、政策允许最高净价（每组/整笔分列，按 `--qty` 实际组数扫描）；交叉/非法盘口不出价、`--snapshot` 标陈旧且不给挂单区间；出场提示含分腿规则（先平卖腿）与短腿提前指派处置未核实。**预算按同标的整体计险**（Codex 061/062）：候选腿并入该标的现仓（现金流水口径）重算，列「拟建后最大亏损 / 操作前 / 各笔相加上界（旧口径对照）」、整组止损依据、整体收益风险比；可行组数逐个枚举 | 终端 |
| `python -m undertow risk-unit <标的> [--leg=带符号张数:C\|P:行权价:到期 ...] [--cash X]` | **同标的整体风险（只读；Codex 061/062）**：现仓全部腿合成一个风险单元（`joint_bound` 同到期精确 / `conservative_bound` 跨到期保守 / `unbounded` / `unresolved`），给最大亏损与取值点、最大收益、整组止损依据、假设清单；给 `--leg` 时算拟加仓/减仓/部分平仓后的剩余组合与变化量（平仓 = 反向数量；负数写成 `--leg=-2:P:50:2026-10-30`），现金情景默认按长桥买一/卖一逐腿对手价，并给拟操作后的政策 B 整本账。现仓先报现金归属与三把尺子（Codex 063 N1 / 064 §5）：① 本轮累计（含换腿离场腿的现金）② 新阶段入场（计划登记 `stage` 时，按沿用腿内部估值 low/ref/high）③ 从现在起（买一/卖一）；政策 B 用 ② 的保守端或 ①。平掉保护腿可能让风险变成无上限，不默认「平仓 = 减险」。加腿属原交易：全部退出后按整组净现金流用原 id 登记一笔 `trial trade`，不另算胜负 | 终端 |
| `python -m undertow sides <标的> --horizon D [--buy K1/K2C] [--sell K1/K2P] [--round-dte N] [--live]` | 买方 vs 卖方同期对比：卖方按短到期滚动多轮，每轮扣费净额、两点打平胜率、delta 价内概率粗略代理（非含费盈利概率）、额度与下限；N 轮全赢只是情景 | 终端 |
| `python -m undertow sentiment forward\|replay --start D\|show\|audit` | **情绪面板 v0（描述层，Codex 066）**：每品种三组分量分列——波动指数（GVZ/VXSLV/OVX/VXN，qqq 行另附 VIX 背景）、期权不对称与张数（恒 30 日严格 25Δ 偏斜、两侧 50Δ、源链 P/C 0–45D 与全链）、COT 分类持仓（真实类别、毛多毛空、净/OI）。每列带数据日、可得时刻、prior-only 中位秩分位与质量状态（current/expected_carry/pending/stale/missing/invalid/reconstructed/unknown_vintage）。**不合成恐慌分、不给方向、不进研判、不过滤开仓**；分位不是反弹概率 | `data/history/sentiment/{forward,replay}/`、`coverage_audit.json` |
| `python -m undertow riskwatch run\|plan-show\|plan-set\|keys\|deliver` | 盘中持仓风险巡检（实盘 + 模拟仓 + 账户）：有新事项才提醒，同日同事项只报一次；`plan-set --campaign-start ISO` 登记本轮起点、`--stage-start ISO --stage-inherit 代码:张数:low:ref:high [--stage-basis …]` 登记换腿新阶段（Codex 063 N1）；`session_hooks.sh` ⑯ 交易日 ET 09:45–15:45 每 30 分钟自动跑 | 私有 `data/account/riskwatch/`、`data/account/position_plans.json` |
