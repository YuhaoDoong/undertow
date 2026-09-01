# codex review 2026-09-01（gpt-5.6-sol, reasoning=high）

> 审查范围：2026-09-01 当天对「期权结构/墙位、卖方价差、极强信号、买方策略」
> 四块的整理，以及当天修复的三处口径事故。
> 结论：**5 个 P0 + 7 个 P1 + 3 个 P2**。原话：「测试全绿不代表可交付，
> 至少四个 P0 应先阻断。」

## 修复状态（2026-09-01 收盘前）

| 编号 | 问题 | 状态 |
|---|---|---|
| P0-2 | `p_value=None` 只修 status 未修渲染，**当日研报会 TypeError 崩溃** | ✅ 已修 + 4 条回归测试 |
| P0-4 | `should_exit` 距离判断无方向，深度破墙时**拒绝平仓** | ✅ 已修 + 3 条测试 |
| P0-3 | 只清绩效数字、策略参数仍是污染网格所挑，白银仍 ACTIVE | ✅ 已停用全品种 + `spot` 改必填 |
| P0-1 | 快照 `captured_at` 被丢弃，191 份里 21 份盘后抓取仍当盘前用 | ⬜ 未修（设计级） |
| P0-5 | band 缺陷贯穿 `analyze_gamma` 主墙/持续墙/分层墙，非仅卖方模块 | ⬜ 未修（设计级） |

P1/P2 全部未修，见下文。

---

## P0

- [scripts/backtest_wall_spread.py:35](/Users/yhdong/Trading/scripts/backtest_wall_spread.py:35)、[store.py:95](/Users/yhdong/Trading/undertow/collect/store.py:95)、[cli.py:2541](/Users/yhdong/Trading/undertow/cli.py:2541)：回测和台账仍按文件名 D 决定可交易日，却完全丢弃 `captured_at`。

  我审计了 191 份快照：21 份工作日快照在 09:30 ET 后抓取，其中18份已在收盘后。新回测仍把它们当成 D 开盘前已知，并用 C[D−1]“开仓”；等于拿 D 收盘后期权链，回到 D 开盘交易，仍是前视。正规信号台账重建也受同一问题影响。

  怎么改：`SnapshotStore` 必须提供含 `captured_at` 的读取接口；按捕获时刻确定 `decision_session`。盘前快照→D，盘中/盘后快照→下一交易日，无法模拟准确成交的盘中快照应剔除。基准价取决于 `decision_session`，不能再固定解释为文件名 D−1。

- [validation.py:75](/Users/yhdong/Trading/undertow/analyze/validation.py:75)、[validation.py:108](/Users/yhdong/Trading/undertow/analyze/validation.py:108)、[html.py:1394](/Users/yhdong/Trading/undertow/report/html.py:1394)：`p_value=None` 只修了 `status`，没有修完整渲染路径。

  `gate_net_effect.hits=None` 会让 `rate` 做 `None / n`，HTML 又会格式化 `None` 的 p 值。实测：

  - `render_validation_table()` → `TypeError`
  - `badge("gate_net_effect")` → `TypeError`

  综合研报必经 [html.py:1529](/Users/yhdong/Trading/undertow/report/html.py:1529)，因此今天的每日研报会崩。

  怎么改：为“无法检验”增加独立展示分支，不访问 `rate/need_more`，p 显示“—”；同时测试 `badge()`、`render_validation_table()` 和完整 `render_report_html()`。

- [wall_spread.py:53](/Users/yhdong/Trading/undertow/analyze/wall_spread.py:53)、[wall_spread.py:83](/Users/yhdong/Trading/undertow/analyze/wall_spread.py:83)、[wall_spread.py:213](/Users/yhdong/Trading/undertow/analyze/wall_spread.py:213)：只清空了六个结果数字，没有作废用污染回测选出的策略参数。

  `cap/band/width_pct/dte/sides/confirm` 仍是错位 `snapshot.spot` 网格挑出来的，白银还保持 `ACTIVE`。若找到候选，理由字符串会继续对 `None` 做百分比格式化并崩溃。测试甚至在 [test_wall_spread.py:108](/Users/yhdong/Trading/tests/test_wall_spread.py:108) 锁死“白银已激活”。

  怎么改：在捕获时序、墙定义和新回测全部通过前设 `ACTIVE=set()`；所有校准参数一起作废，不能只清绩效栏；`propose()` 的 `spot` 改为必填，不允许回退 `snap.spot`。

- [wall_spread.py:221](/Users/yhdong/Trading/undertow/analyze/wall_spread.py:221)、[test_wall_spread.py:37](/Users/yhdong/Trading/tests/test_wall_spread.py:37)：`should_exit` 没有实现“已越过卖腿”，而且距离判断在严重破墙时方向反了。

  当前只判断绝对距离≤5%。因此：

  - 卖 60C、现价59，尚未破墙，却会平仓；
  - 卖 60P、现价50，已经深度破墙，却因距离>5%拒绝平仓。

  后者会在最大风险时阻止退出。测试正把前一种错误行为锁成正确。

  怎么改：先做有方向的破墙判断：put 要求 `spot < sell_strike`，call 要求 `spot > sell_strike`。若保留距离条件，只能用于“尚在安全侧时不响应”，不能拒绝已经向不利方向深度破墙的仓位。

- [gamma.py:190](/Users/yhdong/Trading/undertow/analyze/gamma.py:190)、[gamma.py:402](/Users/yhdong/Trading/undertow/analyze/gamma.py:402)、[gamma.py:475](/Users/yhdong/Trading/undertow/analyze/gamma.py:475)：已知 `accum_wall` 的 band 缺陷实际上贯穿全部核心墙位，不只卖方模块。

  `analyze_gamma` 主墙、持续墙、近/中/远分层墙都在相对带内取最大值。SLV 现价60时，真正的50 put墙连±15%都在范围外，核心报告同样会把较小的局部档位叫作“墙”。[test_gamma.py:70](/Users/yhdong/Trading/tests/test_gamma.py:70) 还明确锁住了这种行为。

  怎么改：拆成两个概念：全范围、带绝对量/占比门槛的“结构主墙”；近价带内的“局部 pin/最近阻力”。不得都叫墙，更不能让局部相对最大反转结构主墙。

## P1

- [backtest_wall_spread.py:86](/Users/yhdong/Trading/scripts/backtest_wall_spread.py:86)：所谓“新回测唯一出口”没有回测它声称的完整策略。

  它从未调用 `should_exit`，没有逐日持仓或提前平仓；忽略 `confirm`；要求卖腿行权价必须恰好等于墙，而生产 `propose()` 取最近可用行权价；也没有日期簇、置换或 bootstrap，只输出描述性年化。加载快照时 [backtest_wall_spread.py:47](/Users/yhdong/Trading/scripts/backtest_wall_spread.py:47) 还裸吞异常，可能选择性丢样本。

  怎么改：生成逐笔账本，逐交易日推进持仓和退出；与生产候选选择共用同一函数；按进场日期聚类，对净 P&L/资本占用收益做置换或簇 bootstrap，并列出全部排除原因。

- [gate_analysis.py:69](/Users/yhdong/Trading/scripts/gate_analysis.py:69)、[signal_ledger.py:315](/Users/yhdong/Trading/undertow/analyze/signal_ledger.py:315)：当前闸门推断没有满足日期簇要求，所谓抽稀也不保证不重叠。

  `_thin` 文档写“交易日”，实现却用日历日。周五和周一在 horizon=2 时会同时保留，但两者的收益窗口重叠。之后 Welch 和置换仍把跨品种同日观察当独立样本。

  独立重建结果：

  - 89条压力达标记录中，没有任何一行两侧同时达标，因此当前不存在“一行两记”。
  - 10条缺 drift，实际可分析79条。
  - 原始差值：被拦组比开火组高约1.20个百分点。
  - 35个日期簇 bootstrap 95%区间约为 `[-0.20,+2.63]pp`，跨0。
  - 只比较同日同时存在两组的17个日期，符号置换单尾约 `p=0.152`。

  怎么改：台账保存收益终点日期；按真实交易日索引消除重叠；随后整日重采样或簇置换，不能只按品种抽稀。

- [gate_analysis.py:128](/Users/yhdong/Trading/scripts/gate_analysis.py:128)、[validation.py:182](/Users/yhdong/Trading/undertow/analyze/validation.py:182)：组间差异有明显构成偏倚，不能解释成闸门的净效果。

  分层差值“被拦−开火”约为：

  - silver `+3.41pp`
  - WTI `+1.68pp`
  - gold `−0.35pp`
  - 看涨候选 `+2.15pp`
  - 看跌候选 `+0.37pp`
  - 7月 `+2.09pp`
  - 8月 `+0.59pp`

  剔除 WTI 并抽稀后差值缩到约 `+0.61pp`，置换约 `p=0.206`。所以“方向一致”可能来自品种、方向和时间构成；h=2/3/5还是同一批高度重叠收益，并非三次独立复现。

  怎么改：预注册单一主窗口；按品种×方向×regime×月份分层或做日期簇回归/置换；最终开火与“所有被拦”只能描述整体规则关联，不能识别某一道闸门的因果效果。

  你的文字结论“没有证据证明闸门有害”是正确的；但理由应是簇推断不成立，而不是简单的 `|t|<2`。`scale_ok` 0次拦截只能说明它在当前样本中没有增量作用，不能用同一批数据调阈值后再宣称优化。

- [signal_ledger.py:340](/Users/yhdong/Trading/undertow/analyze/signal_ledger.py:340)、[validation.py:146](/Users/yhdong/Trading/undertow/analyze/validation.py:146)：正规台账仍用裸二项命中率作为强信号统计，违反你列出的三条回测铁律。

  `9/16、p=0.804` 本质上仍是对50%的二项检验；没有日期簇，也没有检验方向化净收益。它比手工台账诚实，但还不是合规的验证。

  怎么改：正式输出改为日期簇置换/bootstrap的方向化净标的收益；开发买方策略后，主检验对象必须进一步改成扣权利金、点差、theta和手续费后的期权净 P&L。

- [signal_ledger.py:66](/Users/yhdong/Trading/undertow/analyze/signal_ledger.py:66)、[signal_ledger.py:285](/Users/yhdong/Trading/undertow/analyze/signal_ledger.py:285)：统计读取默认 `strict=False`，损坏的品种台账会被静默当成空表。

  这会让某个品种整段历史从统计中消失而没有警告，重新造成选择偏差。另无 schema/算法版本；未执行全量 rebuild 时，新旧闸门定义可以混在同一文件。

  怎么改：统计入口默认严格读取并失败显式；台账行记录 `schema`、signal/gate版本、`captured_at`和`decision_session`；不同版本不得合并统计。

- [scan_wall_spread.py:53](/Users/yhdong/Trading/scripts/scan_wall_spread.py:53)、[grid_wall_spread.py:34](/Users/yhdong/Trading/scripts/grid_wall_spread.py:34)、[test_signal_exit.py:53](/Users/yhdong/Trading/scripts/test_signal_exit.py:53)：仍有未标废弃的错位 `snapshot.spot` 回测入口。

  三个旧脚本仍可输出数值，[HANDOFF.md:31](/Users/yhdong/Trading/docs/HANDOFF.md:31) 还把它们列为“全部可复现”。另外三个已加警告的旧脚本也只是注释警告，仍会正常运行并打印污染数字。

  怎么改：旧脚本默认直接退出并指向唯一入口，只有显式 `--reproduce-invalid-history` 才允许运行；HANDOFF同步删除推荐。涉及参数扫描的历史结果全部作废。

- [cli.py:1445](/Users/yhdong/Trading/undertow/cli.py:1445)、[HANDOFF.md:22](/Users/yhdong/Trading/docs/HANDOFF.md:22)：文档说 `wall_spread.py` 是当前主线，但报告仍调用旧的 `credit_wall.py`。

  新卖方策略事实上没有接入报告；旧模块虽然安全地停用，但“当前主线已激活”与用户实际看到的内容不一致。

  怎么改：在 v2重新验证前，文档明确标“研究中、未接入、未激活”；验证通过后再替换单一调用链，不能并存两个口径。

## P2

- [tests/test_signal_ledger.py:280](/Users/yhdong/Trading/tests/test_signal_ledger.py:280)：新增 horizon 测试只断言“2在列表里”，没有断言 `forward_2d=C[D+1]/C[D−1]−1`。建议加入周末、节假日及最后一根不足的明确样例。

- [tests/test_validation.py:27](/Users/yhdong/Trading/tests/test_validation.py:27)：只测 `status`，没测 `summary/badge/HTML`，所以402项测试全绿仍漏掉研报必崩回归。

- [validation.py:133](/Users/yhdong/Trading/undertow/analyze/validation.py:133)、[credit_wall.py:65](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:65)：没有发现另一份仍被正式统计读取的手工历史数据文件；`strength_backtest.json`、`regime_predict.json`、containment、ratio_watch等都有生成入口。但大量 n/p/CI 仍手工复制进 Python 常量，登记簿很容易和生成结果漂移。应让脚本输出带 schema 的机器可读 artifact，报告读取 artifact，代码只定义状态规则。

## 对 C 的明确结论

`backfill()` 内部的索引公式是正确的：[signal_ledger.py:242](/Users/yhdong/Trading/undertow/analyze/signal_ledger.py:242) 取严格早于 D 的最后收盘，`i+2` 确实对应 D+1；现有183行中没有 `base_date >= date`，169行已经填入 `forward_2d`。

但它只在“文件名 D 就是盘前决策日”成立时无前视。历史快照的 `captured_at` 已证明该前提不成立，因此应评价为：公式修对了，执行日期仍错，整体前视尚未消除。

验证情况：`402 passed`，编译通过；无第三方运行时导入、无券商下单命令、敏感文件检查无命中。测试全绿不代表可交付，至少四个P0应先阻断。
