# codex review 2026-09-04 —— TA 有效性检验方法

> 完整会话日志略，以下是最终结论部分。

结论先说：**当前统计方法不足以支撑“TA 指标无效”或“美元上五个指标明确有害”这两个结论。**

“暂不让 TA 进入研报/方向投票”可以作为保守的产品边界继续保留，但理由只能是“尚未用合格方法证明存在增量价值”，不能写成已经证伪。

## P0

### P0-1：把不同时间尺度、不同样本区间合并，检验对象本身不一致

位置：

- [scripts/validate_ta_clustered.py:74](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:74)
- [scripts/validate_ta_clustered.py:82](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:82)
- [scripts/validate_ta_by_symbol.py:64](/Users/yhdong/Trading/scripts/validate_ta_by_symbol.py:64)
- [undertow/analyze/ta/frames.py:40](/Users/yhdong/Trading/undertow/analyze/ta/frames.py:40)

即使最新版已经按品种拆开，仍然把 1h、4h、1d 合在一起：

- `+3 根`分别代表约 3 小时、12 小时、3 个交易日；
- 默认窗口分别是 250、200、300 根，覆盖的日历区间不同；
- 4h 还是从 1h 聚合而来，与 1h 高度重叠。

因此，单品种的“段数 21～107”也不能当作 21～107 个独立同分布观测。尤其“UUP 五个指标全部显著劣于”仍可能只是某个周期或某段行情驱动的 Simpson 效应。

必须先分别报告 `(品种, 周期, horizon)`。如果要汇总，应使用预先规定的权重和层级/同步时间 bootstrap，而不是直接拼接样本。

### P0-2：段级 bootstrap 没有正确检验“指标相对一直做多的增量”

位置：

- [scripts/validate_ta_clustered.py:40](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:40)
- [scripts/validate_ta_clustered.py:60](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:60)
- [scripts/validate_ta_clustered.py:87](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:87)
- [scripts/validate_ta_clustered.py:96](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:96)
- [scripts/validate_ta_by_symbol.py:86](/Users/yhdong/Trading/scripts/validate_ta_by_symbol.py:86)

当前方法：

1. bootstrap 指标连续同向段；
2. 得到指标命中率 CI；
3. 与一个固定的样本期上涨比例比较。

问题有三个：

- 基线来自全部 K 线，指标来自 warm-up 后的可用 K 线，两者不是完全相同的观测集合；
- 基线也是同一批数据估出来的随机量，却被当成无误差常数；
- `close[i+3]/close[i]` 的窗口跨越信号翻转边界，因此相邻的两个“段”仍共享未来价格，段与段并不独立。

正确零假设应是：

\[
E[\text{指标命中}-\text{同期一直做多命中}]=0
\]

在每个完全相同的可用时间点计算配对差值，再对按时间排列的差值做 moving-block 或 stationary bootstrap。至少应保证块长不小于 horizon，并根据自相关敏感性测试更长块。

跨品种汇总时，要在同一日同步抽取所有品种；跨周期最好不要直接汇总，或采用层级 bootstrap。

关于段长差异：当前代码不是简单“每段命中率等权平均”，而是重采样段后再用 `总 hits / 总 bars`，所以长段仍有更大权重。真正的问题不是单纯的等权，而是把内生形成、相邻仍相关的趋势段当成 iid 簇。

### P0-3：登记簿把尚未成立的统计结论固化成产品边界

位置：

- [undertow/analyze/validation.py:282](/Users/yhdong/Trading/undertow/analyze/validation.py:282)
- [undertow/analyze/validation.py:289](/Users/yhdong/Trading/undertow/analyze/validation.py:289)
- [undertow/analyze/validation.py:291](/Users/yhdong/Trading/undertow/analyze/validation.py:291)
- [undertow/analyze/validation.py:309](/Users/yhdong/Trading/undertow/analyze/validation.py:309)

`“金银原油全部无区别／美元全部显著劣于”`仍依赖上述错误的跨周期汇总、固定基线比较和未校正多重检验。

因此：

- “暂不进入投票”可以保留；
- “美元上明确有害”应撤回；
- “全部与基线无区别”只能改成“当前检验未建立稳定的样本外增量价值”。

## P1

### P1-1：基线概念合理，但当前零假设不完整

位置：[scripts/validate_ta.py:91](/Users/yhdong/Trading/scripts/validate_ta.py:91)

“样本期一直做多命中率”适合回答：

> 使用指标方向，是否比同期永远做多更准确？

但不适合回答：

> 指标是否包含方向预测信息？

因为一个与未来完全独立、但一半做多一半做空的指标，在上涨率 53.9% 的市场中预期命中率约为 50%，自然会输给一直做多。这说明它没有战胜趋势基准，不等于存在负向预测能力。

建议并列两个检验：

- 业务检验：配对比较指标与同期一直做多；
- 信息检验：在保持信号多空比例、连续段结构和收益自相关的前提下做块置换，检验信号与未来方向是否独立。

50% 只适合回答平衡方向准确率，不应代替一直做多基准。

### P1-2：功效说明低估了所需效应

位置：[undertow/analyze/validation.py:304](/Users/yhdong/Trading/undertow/analyze/validation.py:304)

文案称“标准误约 3pp，只能检出 ≥5～6pp”。5～6pp 只是约等于：

\[
1.96\times SE
\]

即“观测值刚好越过显著性门槛”，不是通常所说的 80% power。

若 SE≈3pp，双侧 α=0.05、80% power 的近似最小可检效应是：

\[
(1.96+0.84)\times3\%\approx8.4pp
\]

所以现有数据大致只能对 8～9pp 级效应有较可靠功效。2～4pp 看不见，5～6pp 也只有约一半机会检出。

更可靠的功效应在最终的时间块 bootstrap 设计下，通过注入不同真实效应做模拟。

### P1-3：多重比较和 horizon 选择均未处理

位置：

- [scripts/validate_ta.py:17](/Users/yhdong/Trading/scripts/validate_ta.py:17)
- [scripts/validate_ta_clustered.py:16](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:16)
- [scripts/validate_ta_by_symbol.py:61](/Users/yhdong/Trading/scripts/validate_ta_by_symbol.py:61)
- [scripts/validate_ta_by_symbol.py:91](/Users/yhdong/Trading/scripts/validate_ta_by_symbol.py:91)

先查看多个指标、多个周期、1/3/5 根 horizon，之后固定展示 `K=3`，存在选择后推断问题。最新版又做了 5×4=20 个品种指标检验，却用逐项 95% CI 宣称 UUP 五项显著。

需要预先指定一个 primary horizon，或至少做 Holm/FDR 校正，并将高度同源的指标作为同一检验家族。

### P1-4：普通 Stoch 没看到前视，但 MTF 分支根本没被验证

位置：

- [scripts/validate_ta.py:59](/Users/yhdong/Trading/scripts/validate_ta.py:59)
- [scripts/validate_ta_clustered.py:35](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:35)
- [scripts/validate_ta_by_symbol.py:29](/Users/yhdong/Trading/scripts/validate_ta_by_symbol.py:29)
- [undertow/analyze/ta/stoch.py:140](/Users/yhdong/Trading/undertow/analyze/ta/stoch.py:140)

三份验证脚本调用的都是 `stoch_kd()`，完全没有调用 `stoch_linreg()`、`align_mtf()` 或 `read_mtf()`。因此这些结果不能覆盖 MTF Stoch。

已检查的 ST、UT、MACD、DMI 和普通 Stoch 没看到读取 `i+1` 等直接前视。

MTF 当前还有一个保守错位：

- [undertow/analyze/ta/stoch.py:162](/Users/yhdong/Trading/undertow/analyze/ta/stoch.py:162) 用低周期的开盘 `ts`；
- 但本周期 `close[i]` 只有在其 `close_ts` 才可知。

这不会泄露未来，反而可能把恰好同时收盘的父周期值延迟一个低周期 bar。回测 MTF 时应按“信号实际计算时刻”即低周期 `close_ts` 对齐。

### P1-5：成交口径可能产生乐观偏差

位置：[scripts/validate_ta.py:97](/Users/yhdong/Trading/scripts/validate_ta.py:97)

信号用 `close[i]` 计算，再把 `close[i]` 作为收益起点。作为“收盘后对未来方向的条件预测”可以接受；若解释成可交易收益，则是假设看完收盘价后还能以同一收盘价成交。

进入交易级验证时应改成：

- 信号在 i 根收盘确认；
- 次根 open 成交；
- 加点差/成本；
- 从次根 open 测持有期收益。

### P1-6：验证脚本会静默遗漏品种/周期

位置：

- [scripts/validate_ta_clustered.py:78](/Users/yhdong/Trading/scripts/validate_ta_clustered.py:78)
- [scripts/validate_ta_by_symbol.py:67](/Users/yhdong/Trading/scripts/validate_ta_by_symbol.py:67)

`except Exception: continue` 会让取数或代码错误静默变成样本减少。最终输出仍可能被登记为四品种×三周期。

这直接违反仓库“本该发生却没发生必须告知”的规则。输出必须列出每个 strata 的成功/失败状态，并在缺任何预期序列时将整体结果标为 partial。

### P1-7：`samples_to_significance()` 的搜索上界确实会漏解

位置：[undertow/analyze/validation.py:71](/Users/yhdong/Trading/undertow/analyze/validation.py:71)

存在两个确定问题：

1. `z` 固定为 1.96，传入非 0.05 的 `alpha` 时上界错误；
2. `approx > cap` 直接返回 `None` 没有数学保证。

我用当前实现找到了反例：

```text
p0=0.01, hits=4, n=110, cap=500
函数返回 None
逐点精确搜索在下一样本即找到解：m=111
```

原因是精确二项的离散跳跃与 `ceil(rate*m)` 会使显著性边界不单调贴合正态近似。

既然 cap 只有 5000，最稳妥的是直接从 `n` 搜到 `cap`；性能不是这里的核心瓶颈。若保留上界，必须证明其为严格上界。

### P1-8：`hits 有值、p_value=None` 虽不再崩，但语义仍错误

位置：

- [undertow/analyze/validation.py:128](/Users/yhdong/Trading/undertow/analyze/validation.py:128)
- [undertow/analyze/validation.py:167](/Users/yhdong/Trading/undertow/analyze/validation.py:167)
- [undertow/report/html.py:1403](/Users/yhdong/Trading/undertow/report/html.py:1403)

TA 条目会同时出现：

- `status == "无法检验"`；
- summary 又写“以 bootstrap CI 判定”；
- HTML 因 `need_more=None` 显示“命中率贴近基准，难以证实”；
- `cluster_n=397` 被 summary 称为“日期簇”，实际是混合的连续信号段；
- `1328/2733、baseline=0.539` 是已经承认无效的合并口径，但 note 改成了按品种结果。

应把每个 `(指标, 品种, 周期, horizon)` 作为独立结果记录，保存 effect、CI、bootstrap 类型及有效块数；不要再用一个旧的 aggregate `hits/n` 代表按品种结论。

### P1-9：Gamma/Theta 算术符号正确，但 0DTE 被算成零

位置：

- [undertow/analyze/portfolio.py:230](/Users/yhdong/Trading/undertow/analyze/portfolio.py:230)
- [undertow/analyze/portfolio.py:256](/Users/yhdong/Trading/undertow/analyze/portfolio.py:256)

以下计算本身正确：

- `gamma × 100 × quantity`：组合 Delta 每当标的变动 $1 的变化量；
- `theta × 100 × quantity`：美元/自然日；
- 卖方 `quantity<0` 会反转单腿 Gamma/Theta 符号；
- `CONTRACT_MULT=100` 对当前美股 ETF 期权适用。

但是 `T=max(dte,0)/365` 在到期日盘中令 `T=0`，于是最危险的 0DTE Gamma/Theta 被显示为零。至少应使用剩余实际时间；没有时刻数据时也应明确标为“0DTE Greeks 不可可靠估计”，不能显示零风险。

Gamma 展示还应标明单位，例如“Δ股/$”，否则 `净Γ +1.7` 含义不清。

### P1-10：报告把信用/借记与 Gamma 符号绑定，金融语义不成立

位置：

- [undertow/analyze/portfolio.py:250](/Users/yhdong/Trading/undertow/analyze/portfolio.py:250)
- [undertow/report/markdown.py:541](/Users/yhdong/Trading/undertow/report/markdown.py:541)
- [undertow/report/html.py:2058](/Users/yhdong/Trading/undertow/report/html.py:2058)

“贷方价差=负 Gamma、借方价差=正 Gamma”不是恒等关系。价差的净 Gamma 会随现价、到期、两腿 IV 改变符号。

用仓库 BS 实现举例：一组 `short 100P + long 95P` 的 bull put credit spread，30D、IV 30%，当标的在 90 时：

- 净 Gamma 约 `+1.76 Δ股/$`；
- 净 Theta 约 `−$1.96/天`。

正好与文案的绑定相反。代码算出的瞬时 Greeks 可以保留，但解释必须只根据当前实际符号，不能根据“贷方/借方”推断。

同理，“正 Gamma 时跳空对你有利”也忽略了初始 Delta；正 Gamma 只代表凸性有利，不保证任意方向、任意幅度的跳空都盈利。

### P1-11：`min_dte=1` 只排除了今天到期，和“持有 2～4 天”的理由不一致

位置：[undertow/analyze/gamma.py:788](/Users/yhdong/Trading/undertow/analyze/gamma.py:788)

排除 0DTE 是合理的，但如果卖方价差计划持有 2～4 天，DTE=1、2、3 的墙同样会在持仓结束前消失。`min_dte` 应与预期持有期限或目标期权到期关联，而不应固定为 1。

此外历史检验仍使用默认 `min_dte=0`：

- [scripts/step1_wall_rules.py:68](/Users/yhdong/Trading/scripts/step1_wall_rules.py:68)
- [scripts/backtest_wall_spread.py:105](/Users/yhdong/Trading/scripts/backtest_wall_spread.py:105)
- [scripts/placebo_wall_value.py:106](/Users/yhdong/Trading/scripts/placebo_wall_value.py:106)

因此 gamma.py 注释中原有覆盖率/破墙率不能直接作为新 live 规则的校准成绩。必须用完全相同的 `min_dte` 规则重跑。

## P2

### P2-1：`binom_p()` 常见极端值可用，但边界与“双侧”定义需修正

位置：[undertow/analyze/validation.py:24](/Users/yhdong/Trading/undertow/analyze/validation.py:24)

我核算了：

- `k=0,n=10,p=.5` → 0.001953125，正确；
- `k=n` 对称，正确；
- `p0` 很接近 0/1 时，常见输入也正常；
- 大 n 不再发生 `math.comb` 溢出。

仍有三个边界问题：

- `p0==0` 或 `p0==1` 当前无论 k 是多少都返回 1；退化零假设下，不可能事件应返回 0；
- 未验证 `0 <= k <= n`；
- 当前实现是 `2×较近单尾` 的 equal-tailed p 值，不是常见软件中“累计所有 PMF≤观测 PMF”的 probability-ordering exact p。两者在 `p0 != .5` 时可能不同。

这不影响当前约 0.5 基线的主要判断，但 docstring 应明确采用哪种双侧定义。

严格说实现只是用 lgamma 计算每项 log-PMF，之后仍在普通域求和，不是完整的 log-sum-exp；极小尾部会直接下溢为 0。用于判断 `<0.05` 通常足够，但不能声称任意极端参数下都给精确非零 p 值。

### P2-2：结构墙关于“0DTE pin 效应是真的”的措辞太确定

位置：[undertow/analyze/gamma.py:675](/Users/yhdong/Trading/undertow/analyze/gamma.py:675)

能确定的是 0DTE OI 收盘后消失，不能仅由 OI 确定它当日一定产生 pin。建议改成“可能影响当日 pin/对冲流，但不能支撑跨日墙位”。

## 最终判断

- **“没有一个显著优于基线”**：当前方法下的描述性结果，但不能作为可靠总体结论。
- **“三个/五个显著劣于一直做多”**：站不住；跨周期相关、固定基线、不同样本窗口、多重比较均未解决。
- **“TA 暂不进方向投票”**：可以保留，但这是证据门槛政策，不是“已经证明无效”。
- **现有功效**：若 SE 约 3pp，80% power 大约只能识别 8～9pp 效应；现有数据完全可能漏掉 2～6pp 的真实增量。

未执行完整测试：当前解释器没有安装 `pytest`（`python3 -m pytest` 返回 `No module named pytest`）。以上结论来自源码审查及直接数值探针；没有修改仓库文件。
