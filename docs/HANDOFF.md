# undertow 交接说明（2026-09-01 晚间重写）

给**其他会话/其他人**快速接手用。按「现在能不能用 → 怎么跑 → 已知的坑」组织。

## 0. 一句话现状

**墙位卖方价差策略当前全品种停用。**极强信号未通过验证。
2026-09-01 这一天连续发现三处口径事故 + codex 查出 5 个 P0，
在两个前提（快照捕获时序、墙的定义）修好之前，本仓库**不产出可执行的卖方价差候选**。

能用的只有描述性的东西：期权结构展示、账户复盘、健康检查。
**任何"某策略年化 +XXX%"的数字，出自 2026-09-01 之前的回测，一律不可引用。**

## 1. 必读

```
docs/codex_review_2026-09-01.md         最新审查（5 P0 + 7 P1 + 3 P2）与修复状态
undertow/analyze/signal_ledger.py       文件头 = 信号台账口径唯一真源
undertow/analyze/wall_spread.py         文件头 = 卖方价差的作废声明与重开条件
memory/output-is-skill-not-disclaimer.md 守则：产出是可用 skill，不是免责声明
docs/daily/2026-09-01.md                当日日报
```

## 2. 核心模块

```
undertow/analyze/signal_ledger.py  信号台账：record 落盘 / backfill 回填 / summarize 统计
                                   HORIZONS=(1,2,3,5,10)；forward_Nd 的 N 是从 C[D−1]
                                   起数的收盘根数 → forward_1d=信号当天，forward_2d=次日
undertow/analyze/validation.py     验证登记簿：任何进入决策的判断必须在此登记 n/p
                                   状态含「无法检验」（p_value=None 是合法状态）
undertow/analyze/wall_spread.py    墙位卖方价差 v2 —— ⛔ ACTIVE=set()，全品种停用
undertow/analyze/credit_wall.py    v1，已停用（口径全错，保留作反例）
undertow/analyze/flow.py           detect_strong_signal + 三条核心闸门
undertow/analyze/gamma.py          墙位/分层墙 —— ⚠️ band 缺陷未修，见坑 ③
undertow/report/viz.py             wall_history_svg —— 未接进报告，且图例误导，见坑 ④
```

## 3. 可复现入口（唯一指定）

```
scripts/gate_analysis.py         闸门净效果（--thin/--dedupe-row/--exclude）
scripts/backtest_wall_spread.py  墙位价差回测，决策价=C[D−1]
                                 ⚠️ codex P1：尚未调用 should_exit、无逐日持仓、
                                    无日期簇/置换检验，只输出描述性统计
```

**以下旧脚本的输出数值全部作废**（用 `snapshot.spot` 当开仓价）：
`backtest_credit_wall.py`、`backtest_sell_put_wall.py`、`backtest_wall_lock.py`、
`scan_wall_spread.py`、`grid_wall_spread.py`、`test_signal_exit.py`。
前三个已加文件头警告；后三个尚未加（codex P1 待办）。保留它们仅因
`test_position_alert.py` 要求"结论必须可复现"。

## 4. 口径（违反即结论无效）

1. **决策价 = C[D−1]**（真实日线收盘）。⛔ 永远不要用 `snapshot.spot`——
   46 个 SLV 快照里 34 个（74%）的 spot 不是文件名当天的价。
2. 破墙 = **到期日收盘**越过卖腿，不是盘中触及（差 3~4 倍）
3. 价差按**组合单中价**成交（让 25% 点差），不是两边吃满
4. **换墙不平仓**——换墙只影响新开仓选哪个行权价（2026-09-01 用户纠正）
5. 平仓触发 = 反向极强信号 **且** 已越过卖腿（有方向的判断，见 `should_exit`）
6. 统计按**日期簇**，检验**净收益>0** 而非胜率>50%
7. 零假设**不是 50%**。样本期标的普涨时随机做空只有 39~40% 命中；
   等价做法是 `summarize(detrend=True)` 先减 `horizon × drift_60d`。

## 5. 已知的坑（按严重度）

**① 快照捕获时序未修（codex P0-1）**
191 份快照里 21 份在 09:30 ET 后抓取、18 份已在收盘后，回测仍把它们当
"D 开盘前已知"。**这意味着当前所有回测仍含前视**，包括新写的
`backtest_wall_spread.py`。修法：`SnapshotStore` 暴露 `captured_at`，
按捕获时刻定 `decision_session`（盘前→D，盘中/盘后→下一交易日）。

**② 卖方价差参数与绩效全部作废（已停用）**
不只绩效数字，`cap/band/width_pct/dte/sides/confirm` 同样出自污染网格。
重开需三项全过：① 时序修好 ② 墙定义修好 ③ 新回测过日期簇置换检验。

**③ band 缺陷贯穿全部核心墙位（codex P0-5）**
`accum_wall`、`analyze_gamma` 主墙、`persistent_walls`、分层墙都是
"在现价 ±band 内取最大 OI"——范围内总能找到一个，那不是墙。
实测 SLV 真墙一直是 50（16~20 万张），band=5% 每天选出只有 3 万张的档位；
但不限范围又会选中 SLV call 100（距现价 +65%）这类 LEAPS 堆积。
两头都错。修法：拆成「结构主墙」（全范围 + 绝对量/占比门槛）与
「局部 pin」（近价带内），不得都叫墙。`test_gamma.py:70` 还锁着旧行为。

**④ 墙位历史图的图例误导**
图例写「▲▼ 极强信号 ≥10×」，但 ≥10× 只是压力比一个条件。
白银 ≥10× 有 9 个标记，真正开火的只有 4 个——**看图数信号会数出两倍多**。
修法：开火用实心+级别，未开火用空心+「未过闸门」，图例分两行。

**⑤ 极强信号未通过验证**
正规台账 9/16=56%、p=0.804，全窗口不显著。且 codex P1 指出这仍是
裸二项检验，没做日期簇、没检验方向化净收益，**连"合规的验证"都还不是**。

**⑥ 三条核心闸门从未被检验**
检验需历史逐行权价 ΔOI，免费源拿不到，只能向前累积。
2026-09-01 首次用 79 条样本做了相关性观察：净建仓规模闸门 79/79 零拦截；
开火组 D+1 −0.23% vs 被拦组 +0.97%，但 35 个日期簇 bootstrap 95% 区间
`[-0.20, +2.63]pp` 跨 0，且存在品种构成偏倚（silver +3.41pp、wti +1.68pp、
gold −0.35pp），剔除 wti 后差值缩到 +0.61pp（p=0.206）。
**结论是"没有证据证明闸门有害"，理由是簇推断不成立，不是简单的 |t|<2。**

**⑦ 文档与实现不一致（codex P1）**
`wall_spread.py` 被称为主线，但报告实际调用的仍是 `credit_wall.py`。
新策略事实上没有接入报告。

**⑧ 样本期全程单边上涨**
2026-06-25 ~ 08-31，GLD +7.4%、SLV +9.7%、USO +26.8%。
**整套东西未经跌市验证。**

## 6. 三个硬约束

- **纯标准库零依赖**，不引入第三方包
- **只读券商接口**，`order buy|sell|cancel|replace` 不得出现在仓库
- **付费作者内容绝不入库**：`article/`、`docs/screenshot/`、`docs/author_notes.md`、
  `docs/author_playbook.md`、`data/account/`、`data/soul/` 均已 gitignore。
  提交前必跑：
  `git status --short | grep -iE "article|screenshot|author_|playbook|account/|soul/"`
  （注意用 `author_` 而非 `author`，否则会匹配到 commit header 的 `Author:` 行）

## 7. 数据现状

```
CBOE 快照   GLD 49 / SLV 45 / USO 48 / QQQ 25 / TQQQ·SPY·IWM·TLT 各 4
信号台账    data/history/signals/*.json（record 自动落盘，183 行，8 品种）
            ⛔ data/history/strong_signal_days.DEPRECATED.json 已废弃
               （手工维护，漏记 3 条不应验的极强信号 → 幸存者偏差）
长桥        实时报价 ✓、期权链 ✓（chain 无 OI，要逐个 quotes 才有）
样本区间    2026-06-25 ~ 09-01
```

## 8. 每日流程

```
launchd 定时（~/Library/LaunchAgents/com.yuhaodoong.undertow.*.plist）
  daily        每日快照 + 报告 + 自动 git commit
  lbintraday   长桥 OI 时效验证（全天 15 分钟采样）
  session      会话钩子
⚠️ daily 会自动 commit 数据文件。在改代码期间它可能提交中间状态的数据，
   注意 git log 里的「每日自动更新」提交。

手动：python3 -m undertow report [品种] [--no-snapshot] [--no-live]
      python3 -m undertow account          实盘持仓只读复盘
      python3 -m undertow journal --capture 抓当日成交
      python3 scripts/gate_analysis.py      闸门检验
```

## 9. 下一步（按优先级）

1. 修 P0-1 捕获时序 —— 不修，所有回测都还带前视，做什么都是白做
2. 修 P0-5 墙的定义 —— 拆结构主墙 / 局部 pin
3. 用修好的口径重跑卖方价差参数网格，过日期簇置换检验后才谈重开
4. 信号台账的主检验改成日期簇置换 + 方向化净收益（codex P1）
5. 图例修正 + 墙位历史图接进报告
6. 买方策略：已测 n=28、赔率 2.0 但自助法 95% CI `[-3.8%, +23.9%]` 含 0，
   且去掉最好 3 笔后均值归零 —— 不能开
