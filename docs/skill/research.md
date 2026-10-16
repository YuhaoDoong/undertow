# 研究纪律、台账与模块地图

<!-- 2026-10-15 重组时新增。入口见仓库根 SKILL.md；写代码的约定以 AGENTS.md 为准。 -->

## 研究纪律（摘要，原文在 AGENTS.md 第四节）

- **口径先提交，再运行。** 研究脚本的口径、参数、样本切分在运行前提交；运行目录已存在就拒绝覆盖；看过结果之后的改动写明「运行后修改」。
- **探索 ≠ 确认。** 探索性结果（如 `flow-accuracy-explore-v1`）不改冻结规则；正式检验按预登记（`docs/prereg/`）与冻结族（dirledger 族 D，2026-12-31 首次正式运行）。
- **两把尺子。** 单品种时序用二项检验/Wilson；跨品种合并必须按日期聚类 bootstrap。
- **按行情分层。** 大波动日与横盘日分开报告；按当日实际波动分层是事后信息，只能描述，不能直接执行。
- **未知就是未知。** 拿不到当时证据的字段记 unknown，不回填、不折零。
- **墙有没有用要对比基线。** 同距离、同持有期、无墙的历史概率基线 + 判断日聚类样本量；不给基线既不说有效也不说无效。

## 台账一览

| 台账 | 命令 / 脚本 | 位置 | 性质 |
|---|---|---|---|
| 影子账 v5（墙选腿 A vs 距离 B） | `undertow shadow …` | `data/history/shadow/` | 前瞻，12/31 正式检验 |
| 强信号台账 | `undertow signals` | `data/history/signals/` | 前瞻累积 |
| 方向判断台账 | `undertow dirledger` | `data/history/` + 私有作者判断 | 冻结族 D |
| 价格结构研究 | `scripts/ps_*.py` | `data/backtest/ps-*/` | 探索，Codex 审定后才跑 |
| 持仓流准确性 | `scripts/flow_accuracy_explore.py` + `flow_accuracy_availability.py` | `data/backtest/flow-accuracy-explore-v1/` | 探索 |
| 模拟仓 | `scripts/paper_trades.py`、`paper_book.py` | 私有 `data/paper/`、`data/soul/journal.json` | 私有 |
| 实盘试行 | `undertow trial` | 私有 `data/account/` | 正式风控状态 |
| 盘中风险巡检 | `undertow riskwatch` | 私有 `data/account/riskwatch/` | 运行状态 |

## 模块地图（2026-10-15）

```
undertow/
  core/      models / config / clock / calendar / market_calendar
  collect/   数据源适配（CFTC/CBOE/Yahoo/FRED/FairEconomy/长桥只读）+ 快照仓库 + CAS + lb_budget 限额
  analyze/   纯计算：持仓、期权结构、资金流、风控政策（risk_policy 唯一来源）、价差经济量（spread_econ 唯一来源）、
             试行资本与对账、风险巡检规则、价差定价……（逐文件见 analyze/README.md）
  report/    markdown / html / viz / v2
  consult/   咨询上下文包 + 本地只读 HTTP
  soul/      交易体系 / 计划 / 日记（代码公开、数据私有）
  cli.py           主命令编排（研判、数据、账户类）
  shadow_cli.py    影子账命令
  trial_cli.py     实盘试行命令
  dirledger_cli.py 方向台账命令（冻结文件，不改）
  riskwatch_cli.py 盘中风险巡检命令
  desk_cli.py      下单定价工具（spread-quote / sides）
scripts/     调度脚本（launchd 轮询 + 脚本判美东窗口）与研究运行器
```

## 待模块化清单（交 Codex 排序，未实施的不假装已做）

1. `undertow/cli.py` 约 4300 行，按领域拆分到子模块（研判 / 数据 / 账户 / 研究），`cli.py` 只留注册与兼容导出。
2. 模拟仓 `scripts/paper_trades.py`（约 1800 行）是生产路径却放在 scripts/：迁入 `undertow/paper/`，`scripts/paper_trades.py` 留薄壳以保持 launchd 路径不变。
3. `scripts/paper_trades.py --help` 在没有私有日记文件时直接崩溃（先读文件再解析参数），应先解析参数、缺文件给明确提示。
