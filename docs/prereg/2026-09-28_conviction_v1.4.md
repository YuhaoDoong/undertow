# 预登记（草案 v1.4，待 Codex 验收后冻结）：统计合同的两处样本资格修订（Codex 021）

v1.3 的其余内容不变，v1、v1.1、v1.2、v1.3 均原样保留。计算器仍以 `undertow/analyze/direction_stats.py` 为唯一来源，只用合成数据验证过。

## 021-01：预测资格与结果资格分开

- **published**：决策时已合格发布的方向事件，即 eligible 且 s = ±1，与结果是否成熟无关。
- **不重叠冷却**只由 published 事件决定：
  - 从未合格发布的行（eligible = False）**不占**冷却；
  - 已发布但结果缺价或未成熟的事件**照样占**冷却。这是事前定下的政策，目的是不让后续数据是否可得改写事件序列；这类事件本身因 r 为 None，不进入统计。
- **usable**（进入统计）= 决策时合格 + r 已成熟 + regime、s 已知。
- 测试：`test_ineligible_direction_row_does_not_occupy_cooldown`、`test_published_event_with_missing_result_still_occupies_cooldown`（h = 5 与 h = 10 各一例）。

## 021-02：共同支持在原始唯一日期上冻结

- `support(rows)` 在原始面板的**唯一 (品种, t)** 上统计：层内 ≥ 1 个保留事件，且 ≥ 5 个**唯一**非事件可用日。由此冻结合格层集合与最终可比事件集合。
- **点估计与每次重抽都只在这组冻结的层与事件上计算。**重抽只改变权重（副本按出现次数计入）和 μ_k 的重估，**原来被排除的层不能因为复制而复活**。
- 某次重抽中，一个合格层如果没有事件或没有非事件对照行，本次不贡献；所有层都不贡献，记为无效重抽。无效比例超过 5%，判为 insufficient（同 v1.3）。
- 最终可比事件数 = `len(support(...)["events"])`，只看 r 是否成熟，不读收益值。
- 测试：`test_duplicated_nonevent_rows_cannot_revive_excluded_stratum`、`test_resample_without_events_or_baseline_is_invalid`；Codex 021 的 probe 复跑后，两个反例均得到正确结果。
