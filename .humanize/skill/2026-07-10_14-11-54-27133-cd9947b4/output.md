# Code Review 结论

项目的分层、零依赖约束、确定性计算方向都很清晰，75 个测试也全部通过。但当前测试主要覆盖“单到期、数据完整、日期对齐”的理想路径，尚未覆盖多到期组合、0DTE、空链、跨源时点和跨日状态机等关键边界。

本次确认了 5 组可能直接改变方向判断或策略状态的高风险正确性问题。建议在解决 P0/P1 前，不把 Flow 倾向、零伽马追认状态和策略卡视为可执行级结论。

审查期间未修改代码；测试结果为 `75 passed`。工作树中原有未跟踪目录 `.humanize/`，本次未触碰。

## 按严重度排序的问题

### P0 — 高危正确性问题

1. Delta/偏斜修正项的符号反了，会把机械 IV 变化放大成主动买盘

位置：[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:561)、[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:599)

当前计算：

```python
corrected = d_iv - slope * d_spot
```

但 `slope` 是横截面的 `∂IV/∂K`。在 sticky-moneyness 近似下，固定行权价的机械 IV 变化约为：

```text
mechanical ΔIV ≈ -slope × Δspot
```

因此去除机械项应是 `d_iv + slope*d_spot`，并最好带上约 `K/S` 的尺度，而不是当前的减法。

合成复现中，put skew 斜率为负、现价上涨 1、原始 IV 机械上涨 1pp；当前实现将其修正成 `+2pp`，两档都判为“买方保护”，而正确残差应接近 0。这会直接污染：

- 买卖方判定；
- `downside_pressure/upside_pressure`；
- Flow 投票；
- `structural_moves`、`counter_signals`；
- 最终综合方向和策略否决票。

此外斜率跨所有到期统一拟合，也把期限结构差异混进了偏斜。

2. Flow 把多个到期合并，随后可能跨到期伪造垂直价差

位置：[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:565)、[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:381)

模块说明声称逐 `(到期, 行权价, C/P)` 分析，但 `_agg()` 实际键只有：

```python
(strike, kind)
```

因此：

- 同一行权价的不同到期被合并；
- OI、IV、Delta 被跨期限加权；
- `FlowChange.expiry` 只保留最早到期；
- `detect_spreads()` 又完全不比较 expiry。

合成案例中，7 月到期的 `100C` 卖方与 8 月到期的 `105C` 买方被识别为 Bear Call Spread。它们不可能是垂直价差。

这也会造成期限组成变化引发的 Simpson’s paradox：即使每个到期 IV 都没发生主动变化，跨期 OI 权重变化也能改变加权 IV，进而触发买卖方判定。

3. 不等量价差会扣掉整条保护腿，而不是只扣匹配数量

位置：[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:405)、[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:639)

`Spread.size` 正确取两腿较小值，但压力调整时扣的是保护腿全部：

```python
p = abs(c.d_oi) * c.weight
```

合成案例：

- 卖 `100C`：+100；
- 买 `105C`：+250；
- 疑似价差匹配规模：100。

当前实现把 250 的看多压力全部清零，输出“偏空”；但剩余 150 张买方 call 应继续作为未配对的方向仓存在。允许的腿比率最高达到 2.5，因此该误差可能很大。

4. 0DTE 在报告生成当天被全部排除，而自动报告恰好在到期前的美东凌晨运行

位置：[gamma.py](/Users/yhdong/Trading/undertow/analyze/gamma.py:128)、[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:291)、[cli.py](/Users/yhdong/Trading/undertow/cli.py:494)、[daily_update.sh](/Users/yhdong/Trading/scripts/daily_update.sh:4)

Gamma 和 Flow 都要求：

```python
0 < T
```

而 `T` 只有日期精度。因此报告日当天到期、但尚未开盘/尚未结算的 0DTE 合约全部被视为已过期。自动任务却在 ET 01:00–08:59 运行，此时这些合约仍然有效，而且通常是 Gamma 最敏感的一层。

仓库的 2026-07-10 快照明确包含 7 月 10 日到期合约。用同一快照做日期敏感性检查：

| 品种 | 当前代码零伽马 | 给当日到期留 1 天 T 的诊断值 |
|---|---:|---:|
| Gold/GLD | 383.46 | 380.48 |
| Silver/SLV | 54.18 | 55.01 |
| WTI/USO | 108.40 | 106.97 |

“一天 T”不是最终正确答案，但足以证明 0DTE 时间处理会实质改变零伽马和净 GEX。应按真实截止时间计算 fractional T，或明确采用“上一完成交易日结构”并统一整条报告的时间口径。

5. 零伽马扫描会为空链制造假根，且策略层假定了未经保存的穿越方向

位置：[gamma.py](/Users/yhdong/Trading/undertow/analyze/gamma.py:90)、[gamma.py](/Users/yhdong/Trading/undertow/analyze/gamma.py:102)、[strategy.py](/Users/yhdong/Trading/undertow/analyze/strategy.py:203)

`prev_G == 0.0` 被直接当作零点。空链、无有效 IV 链或 BS 数值下溢形成的零区间，都会产生伪根。实测空 `OptionsSnapshot` 返回：

```text
zero_gamma = spot
```

随后 Outlook 仍可能围绕这个假翻转位生成情景。

另一个独立问题是 `_find_zero_gamma()` 只返回位置，没有返回穿越方向。策略层却硬编码：

- 零伽马下方 = 负 Gamma；
- 零伽马上方 = 正 Gamma。

这不是数学保证。合成链可得到“下方正 Gamma、上方负 Gamma”；实测该链现价在零伽马上方、净 GEX 为负，但策略文本仍会把上方称为正 Gamma 侧。

零点至少需要携带：

- 左右两侧符号；
- 根区间；
- 是否多根；
- 扫描边界是否截断；
- 数值有效性/最小绝对 GEX。

### P1 — 重要正确性与失败模式

6. 策略窗口状态机会追溯性地宣称“应已持仓”，但没有历史方向、否决票或策略启用状态

位置：[strategy.py](/Users/yhdong/Trading/undertow/analyze/strategy.py:248)、[strategy.py](/Users/yhdong/Trading/undertow/analyze/strategy.py:430)

状态机只接收：

- 当前方向；
- 历史零伽马；
- 历史价格；
- 当前 ATR。

如果今天才从中性/偏多转成偏空，而破位和回抽早已发生，它仍会宣布：

> 追认成立，按规则仓位应已在场内。

但当时可能根本没有做空方向，或当时存在两张否决票。当前 [黄金报告](/Users/yhdong/Trading/data/reports/gold_2026-07-10.html) 就给出了“2026-06-24 或更早破位，按规则应已在场内”的追溯结论。

另外：

- 日期通过交集静默丢失，最新结构日或价格日缺失时没有 stale 状态；
- 当前 ATR 被用于判断所有历史回抽，历史状态可能随未来 ATR 改写；
- `_fade_window_note()` 虽接收 `struct_history`，却用今天的墙判断过去是否“摸墙”，见 [strategy.py](/Users/yhdong/Trading/undertow/analyze/strategy.py:306)。

状态机应基于逐日完整状态事件流：当日方向、否决票、翻转位、ATR、收盘、最高/最低和实际状态转移。

7. 策略总裁决与情景状态存在直接矛盾

位置：[strategy.py](/Users/yhdong/Trading/undertow/analyze/strategy.py:451)

判断优先级先检查否决票，再检查 `armed`。因此即使情景状态已经是“追认成立”，只要有一张否决票，裁决仍写：

> 仅顺结构情景触发后才值得跟进。

当前黄金报告同时展示：

- 顶部：“仅触发后才值得跟进”；
- 情景票：“追认成立，按规则应已在场内”。

这不是措辞问题，而是状态优先级和持仓生命周期不一致。需要区分：

- 新开仓资格；
- 历史已入场后的持仓管理；
- 当前否决票是否要求减仓/退出；
- 情景已失效。

8. “昨日无此行”时默认所有新增 OI 都是买方，并赋予满权重

位置：[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:349)

没有昨日 IV 时：

- 新 put OI → “买方保护”，权重 1.0；
- 新 call OI → “买方”，权重 1.0。

OI 的每一张新增合约同时有买方和卖方；没有成交方向或可比较 IV 时，无法判断谁是主动方。新挂牌行权价、新到期、昨日缺报价或解析缺失都会触发该分支，进而获得最强方向权重。合理结果应是“新 OI、主动方未知”，不进入方向压力，或显著降权。

9. 已识别的价差保护腿仍会被当成“对手盘警示”

位置：[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:184)

`counter_signals()` 只检查 `bias`，不检查 `spread_note`。因此一个已被识别为 Bear Call 长腿保护的看涨 call，虽然在压力聚合中被扣除，仍会在速读中作为独立看多反证。

当前黄金报告真实出现了：

> call 端 4,250 轻微买方（熊市看涨价差·长腿保护）

并将其列为偏空研判的对手盘。这使同一份报告对同一条腿使用了互相矛盾的语义。

10. Yahoo 当前未完成日线被当作日线收盘使用

位置：[yahoo_futures.py](/Users/yhdong/Trading/undertow/collect/yahoo_futures.py:53)、[cli.py](/Users/yhdong/Trading/undertow/cli.py:563)、[strategy.py](/Users/yhdong/Trading/undertow/analyze/strategy.py:67)

Yahoo `1d` 返回中包含当前交易中的日 bar，解析器没有按 `regularMarketTime` 或交易时段过滤。仓库缓存显示报告生成时：

- 最后一根期货 bar 日期为 2026-07-10；
- `regularMarketTime` 约为 ET 01:49；
- 该 bar 的 close 等于实时价，明显尚未完成。

这根部分 bar 被用于：

- “较上一交易日涨跌”；
- ATR；
- 缓冲区和仓位缩放；
- 在日期匹配时可能进入状态判断。

报告又宣称状态基于“已完成日收盘”，时间语义不一致。应明确分离 `completed_series` 与 `live_quote`。

11. 快照、期货价和报告日期没有统一 freshness contract

位置：[cli.py](/Users/yhdong/Trading/undertow/cli.py:374)、[cli.py](/Users/yhdong/Trading/undertow/cli.py:482)

主要问题：

- 只要今天已有快照，`report` 就不会再拉最新期权数据；即使使用 `--no-cache` 也一样；
- `--no-snapshot` 会无条件使用最新历史快照，没有年龄上限；
- 最近两份快照可能相隔多个交易日，但仍以同样权重称作“日对日”；
- 快照存储日期是采集日，经济观察通常对应前一交易日；
- Yahoo 实时期货价可能与 CBOE 闭市 ETF 价相隔数小时，却直接组成“实时比值”；
- 报告标题使用今天，核心结构可能来自更早的链。

应在分析前形成显式的 `DataAsOf` 合同，并对超龄、跨多日、跨时区差异进行拒绝或降权。

12. `cmd_report` 的部分失败仍返回成功，自动化可能提交不完整报告集

位置：[cli.py](/Users/yhdong/Trading/undertow/cli.py:446)、[cli.py](/Users/yhdong/Trading/undertow/cli.py:664)

`cmd_report` 长 245 行，约有 64 个分支性节点、7 个 `try` 块，承担了取数、对齐、分析、状态构建、渲染、归档和写盘。

每个品种由一个大 `try/except` 包围；只要至少一个品种成功，命令最终返回 0。于是 gold 成功、silver/wti 失败时，`daily_update.sh` 仍会继续 commit/push，不会把任务标记为部分失败。

此外：

- 历史结构重算和结构变化分别用裸 `except Exception: continue/pass`，报告里不会出现“该层失败”；
- Yahoo 和 CBOE 价格源同时失败会让整份报告失败，而不是降级为无价格图；
- 错误只有品种级字符串，没有阶段、输入 as-of 或 traceback。

### P2 — 中等风险与可维护性问题

13. 相对 IV 计算还有期限、缺失值和基准选择污染

位置：[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:571)、[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:605)

- IV 只对 `iv > 0` 的合约累加，但除数仍是全部 OI；部分缺 IV 时会系统性压低加权 IV；
- 中位数只来自 `|ΔOI| ≥ 50` 的异常行，不是全链基准，容易受方向性样本选择影响；
- call/put、不同 expiry 使用同一个相对化中位数；
- 当前横截面斜率同时混合不同期限；
- `net_call_doi/net_put_doi` 名称称“净增”，实际只累加正 ΔOI，见 [flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:626)。

14. 10Δ/25Δ 偏斜会在缺少目标 Delta 报价时静默使用端点

位置：[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:426)、[flow.py](/Users/yhdong/Trading/undertow/analyze/flow.py:477)

`_interp()` 对超界目标采取端点截断。如果链只有 20Δ–80Δ 报价，“10Δ skew”实际会使用 20Δ，但报告仍标成 10Δ。到期选择只检查报价数量，不检查目标 Delta 是否被两侧包围。

这会令尾部风险读数看起来比实际更精确。应至少输出实际插值区间、是否外推，以及 coverage 状态。

15. 综合“可信度”只由分数绝对值决定，没有反映数据质量和证据独立性

位置：[outlook.py](/Users/yhdong/Trading/undertow/analyze/outlook.py:387)、[outlook.py](/Users/yhdong/Trading/undertow/analyze/outlook.py:423)

`confidence` 只取决于 `abs(score)`：

- 不看 COT、期权、价格、宏观是否过期；
- 不看缺失层数量；
- 不看方向票是否来自同一数据源；
- Gamma、墙、Flow、IV 实际高度共享同一 ETF 期权链，却按多票呈现；
- 不看回测样本量和超额表现。

建议把“方向强度”和“数据/模型可信度”拆开，不要让“高分”自动等于“高可信”。

16. 速读把 put 墙跌破写成“对冲盘转向助跌”，混淆了 OI 墙与零伽马

位置：[outlook.py](/Users/yhdong/Trading/undertow/analyze/outlook.py:168)

当下方第一层是 put wall 时，文本写：

> 跌破则对冲盘转向助跌。

真正决定净 Gamma 符号变化的是零伽马根，而不是最大 put OI 行权价。put 墙跌破可以意味着支撑失效、pin 解除或保护盘加速，但不能直接推出对冲方向翻转。应分别描述：

- OI 墙失守；
- 零伽马穿越；
- 当前根两侧的实际 Gamma 符号。

17. 快照和报告写盘不是原子操作，且归档顺序可能暂时移除 canonical 文件

位置：[store.py](/Users/yhdong/Trading/undertow/collect/store.py:45)、[cli.py](/Users/yhdong/Trading/undertow/cli.py:429)、[cli.py](/Users/yhdong/Trading/undertow/cli.py:660)

快照直接写目标 gzip；进程中断可能留下损坏文件，而这些快照被定义为不可再生历史。报告则先 rename 旧版，再写新版；如果写入失败，当日 canonical 文件会消失，只剩 `_rHHMM` 归档。

`_score_trend()` 也在报告成功写入前更新历史分数；后续失败会留下“有分数、无报告”的状态。`--json` 模式仍会写报告、归档旧文件并更新趋势，与其他 JSON 命令的只输出语义不一致。

18. 快照“内容指纹”不包含 Flow 真正依赖的 IV/Delta

位置：[cboe_options.py](/Users/yhdong/Trading/undertow/collect/cboe_options.py:90)

指纹只有：

- spot；
- expiry/kind/strike；
- OI；
- volume。

如果 OI、成交量和 spot 相同而 IV/Delta/报价更新，快照会被视为重复并丢弃；错误提示却称“逐行相同”。反过来，少量 volume 更新又可能让旧 OI 链被当成新观察。

19. 交易日映射只跳周末、不识别交易所节假日

位置：[cli.py](/Users/yhdong/Trading/undertow/cli.py:575)

`_chain_day()` 将快照日减一天后只跳过周末。周一节假日后的周二快照会被映射到周一，而非实际的上周五。之后因 Yahoo 没有该日 close，状态机通过日期交集静默丢掉这一天。

## `cmd_report` 的重构边界建议

无需改变四层架构，建议把当前 245 行编排函数拆成以下可独立测试的阶段：

1. `load_report_inputs()`：获取并验证 COT、期权、价格、宏观、事件及各自 as-of。
2. `select_option_observation()`：明确采集日、链交易日、0DTE 截止时间和快照跨度。
3. `build_price_basis()`：只对时间可比的 ETF/期货价生成换算，否则标记估算。
4. `build_structure_history()`：使用交易日历、逐日 ratio、逐日 fractional T，返回覆盖率。
5. `build_report_model()`：只做分析层组合，不做 I/O。
6. `render_artifacts()`：图表失败可局部降级。
7. `commit_artifacts_atomically()`：临时文件写完后统一替换。
8. `ReportRunResult`：记录每个品种每个阶段的成功/降级/失败；部分失败返回非零状态。

应优先消除 [cli.py](/Users/yhdong/Trading/undertow/cli.py:601) 和 [cli.py](/Users/yhdong/Trading/undertow/cli.py:649) 的静默异常。

## 报告内容与信息架构优化建议

### 最高优先级

1. 顶部增加“数据质量条”，并让它参与结论门控

至少展示：

- 期权链经济观察日、采集时间、年龄、合约数、有效 IV 比例；
- 当前是否包含/排除 0DTE；
- Flow 两份快照间隔了几个交易日；
- Yahoo 实时价时间、最新完成日线日期；
- COT 截止日期与发布日期；
- FRED 每个指标各自的日期；
- ETF/期货换算两端的时间差；
- 降级项和失败项。

若关键数据过期，不应继续显示普通“可信度中/高”，而应明确标“数据不足/过期”。

2. 把“今日动作状态”放在 TLDR 后，策略卡移到报告上半部

当前顺序直到接近底部才出现策略裁决。建议顶部明确给一个状态：

```text
当前动作：等待 / 观察触发 / 已入场管理 / 情景失效
方向：偏空（弱）
最近触发：零伽马 4,179，距现价 1.4% / 0.7 ATR
阻断项：1 张否决票
下一事件：CPI，T-4
```

“方向”和“动作”必须分离，避免偏空被误读为立即做空。

3. 合并重复的两套情景

当前同时存在：

- “情景推演”；
- “策略情景参数化”。

两套都在说触发、演化、失效，内容重复且状态语义不完全一致。建议保留一套状态化情景卡；把通用 if-then 作为折叠说明或审计附录。

4. 增加“较上一份干净报告变化了什么”

用一张紧凑表展示：

- 综合分变化及贡献来源；
- 零伽马位移；
- call/put 墙迁移和 OI 增减；
- ATM IV、25Δ/10Δ skew；
- Flow 压力；
- 新增/消失的否决票；
- 策略状态转移。

这比重复展示所有静态信息更适合每日速读。

### Flow/Gamma 信息增强

5. Flow 表应恢复 expiry 维度

当前 [html.py](/Users/yhdong/Trading/undertow/report/html.py:110) 省略 expiry，建议至少增加：

- 到期日和 DTE；
- ΔOI、当前 OI、当日 volume；
- 原始 ΔIV、机械修正项、相对 ΔIV；
- IV/Delta quote coverage；
- 判断置信等级；
- 对方向压力的实际贡献；
- 价差匹配数量与未配对剩余数量。

6. 价差应显示“疑似”证据强度

仅靠相邻行权价、日 ΔOI 和 IV 方向不能证明两腿是同一笔交易。建议展示：

- 同到期检查；
- 匹配比例；
- 成交量一致性；
- 剩余未匹配仓位；
- “高/中/低”结构置信，而不是直接宣布已经扣除。

7. Gamma 卡增加模型不确定性

建议显示：

- 当前净 GEX 数值和符号；
- 零点左右两侧的实际符号；
- 根区间而非只有一个精确点；
- 是否多根；
- 扫描范围；
- 0DTE、近周、远月对净 GEX 的贡献；
- 在替代 dealer-sign 假设下是否稳定；
- 零伽马距现价的百分比和 ATR。

8. 墙位需要显示“成色”，不只显示总 OI

结合项目自己的 R7 规则，可展示：

- 墙上当日 ΔOI；
- 买方保护/卖方写入的估计占比；
- 各 expiry 的 OI 分布；
- 0DTE/近周集中度；
- 墙是否由单个即将到期月份主导；
- 墙位在最近 5 个交易日的迁移。

“put 墙增厚”不应自动等于“支撑更结实”；如果主要是买方保护，破位时可能反而助跌。

### 因子与可信度

9. “按回测可信度加权”旁边直接展示校准证据

当前只有静态权重和“高/中/低”。建议加入：

- 样本量；
- 观察期；
- 5/10/20 日命中率；
- 中位对齐收益；
- 相对无条件基线的超额；
- 最近校准日期；
- 该品种是否有效。

否则“回测可信度”措辞比实际证据展示更强。

10. 把证据按独立数据源分组

建议分为：

- CFTC 独立持仓；
- CBOE ETF 期权结构；
- Yahoo 价格行为；
- FRED 宏观；
- 事件风险。

同一 CBOE 链派生的墙、GEX、Flow、IV 不应视觉上表现为四份完全独立证据。

### 可读性与审计性

11. 建议的新顺序

```text
页眉 + 数据质量
今日动作/策略状态
大白话速读
较昨日变化
关键位 + 价格图
Flow/Gamma 核心证据
最近高影响事件
方向因子与回测依据
COT/宏观详细卡
方法、局限、完整事件表和审计信息
```

12. 事件雷达顶部只保留最近 1–3 个高影响事件

完整 21 天事件表可下移或折叠，否则会把每日核心结构推到较后位置。

13. 增加可复现信息

每份历史报告建议嵌入：

- git commit；
- 配置版本/hash；
- 阈值版本；
- 数据文件名或快照 ID；
- 报告生成时间；
- 观察时间；
- 是否使用缓存/降级源。

否则算法更新后，历史报告之间的差异无法区分是市场变化还是代码变化。

14. 改善移动端和颜色语义

[html.py](/Users/yhdong/Trading/undertow/report/html.py:16) 当前 resistance 为绿色、support 为红色，与报告中多空色彩相反，容易混淆。宽表也没有统一的横向滚动容器。建议统一：

- 阻力/call wall：红或橙；
- 支撑/put wall：绿；
- Gamma flip：紫；
- 不确定/代理：灰；
- 所有宽表提供横向滚动、首列固定或折叠详情。

## 建议补充的测试矩阵

现有 75 项测试全部通过，但建议优先补这些边界：

- Flow：同 strike 多 expiry、跨 expiry 不得配价差；
- Flow：不等量价差只扣 matched size；
- Flow：sticky-moneyness 机械 IV 变化残差应接近 0；
- Flow：部分 IV 缺失不得用全部 OI 作除数；
- Flow：昨日无报价的新 OI 应为主动方未知；
- Gamma：空链、全无效 IV、全 call、全 put、无根、多根、零区间；
- Gamma：反向穿越方向；
- Gamma：0DTE fractional T；
- Vol：10Δ/25Δ 未被报价区间包围时应降级；
- Strategy：当前方向刚翻转时不得追溯宣布已持仓；
- Strategy：历史否决票阻止过入场；
- Strategy：缺最新结构日/价格日必须返回 stale；
- Strategy：历史墙变化时 fade 回看使用当日墙；
- Yahoo：当前未完成 bar 与完成 bar 分离；
- CLI：某一品种失败时的退出码；
- CLI：渲染或写盘失败后 canonical 报告仍完整；
- Snapshot：中断写入、损坏最新快照、连续采集缺日、节假日映射。
