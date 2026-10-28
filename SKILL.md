---
name: undertow
description: >-
  读懂价格表面之下的机构持仓暗流，并服务一个真实的小额期权实盘账户。聚合 CFTC COT 持仓、期权 OI 墙/Gamma、
  买卖方资金流、回测校准的超买超卖与 FRED 宏观，给出黄金/白银/原油/美元/纳指/美债/标普的方向研判与关键位；
  只读接入长桥账户做现价面板、价差挂单定价、买方/卖方同期对比、持仓实时体检与盘中定时风险巡检、
  实盘试行（政策 B）额度与暂停、模拟仓与研究台账。当用户问期货持仓、期权墙、资金流、多空研判、
  事前判断、某个价差怎么挂单、买方还是卖方、持仓风险、账户状态时使用。Use for COT positioning,
  options OI walls / flow, live price panels with ETF↔commodity conversion, spread order pricing,
  buyer-vs-seller comparison, intraday risk watch and read-only review of live option positions.
---

# Undertow — 持仓暗流情报 + 小额实盘工具台

**纯标准库 Python、确定性计算、只读。** LLM 只负责编排与解读，所有数字由代码算出；
任何代码路径都不下单、撤单、改单——执行永远是用户在券商端的动作。

本文件是**入口与导航**：先按下面的「工作流程」找到该跑的命令，细节再按链接读参考文档。
在仓库里写代码的约定见 `AGENTS.md`（与本文件分工：本文件讲怎么用，AGENTS 讲怎么改）。

## 铁律（每次都适用）

1. **只读，绝不交易。** 长桥只用只读子命令（quote/depth/positions/assets/cash-flow/order 查询/kline/option chain/news）。
2. **先列现价再说话。** 事前判断第一行写 ETF 现价（带美东时刻、分场次）+ 用户看的商品价（伦敦银现货 / COMEX / 布伦特 / WTI，标来源与报价时刻）+ 参考比值（两腿同步未核实）；位点两种口径都写。比值 ≠ 持有量。
3. **未校准的不说成已验证。** 证据分级见 `undertow claims` 与 `docs/skill/methodology.md`；探索结果不改冻结规则。
4. **敏感数据不入库。** `data/account/`、`data/soul/`、`data/paper/` 私有；对外文件只写聚合。
5. **仓位上限只有一个来源**：`undertow/analyze/risk_policy.py`（实盘用 `POLICY_B` + `PAYOFF_FLOOR`）。

## 工作流程（按用户的问题路由）

### ① 事前判断（「今晚做空白银/做多原油？」）
1. `python -m undertow quote SLV USO --sessions --commodity --level brent=100` —— 现价面板：常规/盘前/盘后/夜盘分场次带美东时刻，商品价并列与参考比值（标期货报价时刻，两腿同步未核实），`--level` 双向换算位点（可多次）。
2. `python -m undertow walls silver wti` —— 最近几道墙、ΔOI、买卖方向；注意哪些墙是明后天到期（钉价、到期后变薄）。
3. `python -m undertow flow` / `expiry` / `report` —— 持仓流与到期阶梯（持仓流金银略好于抛硬币、原油无信息，见 `data/backtest/flow-accuracy-explore-v1/`）。
4. `python -m undertow news <品种>` —— 新闻只作背景，不改判方向。
5. 外部作者的位点：按来向区分支撑/压力；登记用 `dirledger author-add`（私有），对错只由价格裁定。

### ② 下单定价（「这个价差挂多少？」「买方还是卖方？」）
- `python -m undertow spread-quote USO 2026-11-20 C 150/155` —— 逐腿买一/卖一 → 整单净价三档（对手价/中价/最优）、**组合单挂单区间**（长桥整个价差一起挂）、含费最大亏损/收益、盈亏平衡（ETF 与商品口径）、政策 B 与收益风险比下限、政策允许的最高净价（按 `--qty` 组数算；每组与整笔分开列）。缺盘口 = 不可计算；盘口交叉/非法/报错 = 整单不出价；对手价只是逐腿参考，不保证整单成交。
- `python -m undertow sides USO --horizon 2026-11-20 --buy 150/155C --sell 140/139P [--live]` —— 买方 vs 卖方**同一时间段**对比：卖方按短到期滚动多轮，列每轮扣费净额、两点打平胜率、delta 价内概率粗略代理（不是含费盈利概率；含费最大收益 ≤ 0 时不给）。「N 轮全赢」只是情景，不是期望或回测。
- `python -m undertow trial status` —— 当前净资产与预算权益 R、额度（实盘政策 `LIVE_POLICY`：大仓线 max(15%·R, $150)）、暂停状态；政策通过 ≠ 券商能开（权限、组合占用另行核实）。
- `python -m undertow trial r` —— 预算权益 R 快照：max(0, min(日初基准 + 已登记外部资金, 当前净资产))（Codex 065）；日初基准由收盘后定时任务 `trial r-anchor` 采集，漏采时开盘后 R 不可判定、新增冻结。
- 下单后：`python -m undertow live` 体检，并用 `riskwatch plan-set` 写下止盈/失效位/复核日。

### ③ 持仓风控（「检查我的账户」「有没有风险」）
- **盘中定时巡检（自动）**：`scripts/session_hooks.sh` 在交易日 ET 09:45–15:45 每 30 分钟跑 `python -m undertow riskwatch run`，覆盖实盘与模拟仓；**正常不打扰，只有新告警才通知**（同一事项同一天只报一次，严重度升级才再报）。目的：让用户少看盘、防情绪化交易。
- 手动：`riskwatch run`（同一套规则立即跑一次）、`riskwatch plan-show` / `plan-set`（每笔实盘的止盈、失效位、复核日）、`riskwatch keys`（列出当前实盘价差的计划 key，写计划时用）。
- `python -m undertow live` —— 真实可平仓价（多头按 bid、空头按 ask）与生命周期台账；**止损判定用它，不用 App 的 last**。
- `python -m undertow account` —— 持仓理论评价、多币种现金（外币负余额要提示用户自行换汇）。
- **出场机制**：长桥组合单可以整组平，也可以分腿平——分腿时**必须先买回卖腿、再卖出买腿**（用户 2026-10-15 实测）。买方价差：含费净利润 ≥ 实际入场付出（100%，`debit-roi100-review-v2-20261023`，用户 2026-10-23 / Codex 063）→ 巡检提醒，**讨论后由用户决定平不平**，从不自动平；`plan-set --entry-fees` 写入按现金流水核定的实际入场费，缺省按模型估计；`--qty-entered` 写入按成交核定的入场组数——未登记或与当前组数不同时，提醒只说「整轮 ROI 未核实」（入场费按组数比例分摊为估计）。模拟仓按记录挂的规则版本与生效时刻提醒（v1 不被 v2 覆盖）；可平仓净价超出价差宽度 = 报价异常待复核，不认证止盈。卖方价差 90% 止盈（政策 B）。

### ④ 实盘试行账（政策 B）
`trial init --planned 500` → `trial fund --amount <到账USD> --note "<用户原话>"` → 每笔平仓后 `trial trade`（人工核对券商流水）→ `trial verify-trades`。
`trial reconcile` 只给候选，`--apply` 已停用；暂停（亏损 ≥50% 或连续 4 笔亏损）只有用户明确决定才 `resume`。

### ⑤ 模拟仓
`python3 scripts/paper_trades.py status`；由 `scripts/paper_tick.sh`（launchd 每 60 秒）自动入场、盯市、结算，私有数据在 `data/paper/`。
同向分批是正常做法；模拟仓重新选档算原判断的延续（revision 链）。

### ⑥ 研究与台账（不直接给交易建议）
`shadow`（墙选腿 vs 距离选腿前瞻影子账）、`signals`（强信号台账）、`dirledger`（方向台账，12/31 正式运行）、
`backtest*`、`scripts/ps_*.py`（价格结构研究，运行前提交口径、运行目录不覆盖）。规则见 `docs/skill/research.md`。

### ⑦ 与 Codex/GPT 协作
沟通在 `GPTcom/<批次>/`（不提交）：Codex 规划与书面审查，Claude 实施；「阅读回信」= 读最新批次并按授权实施。

## 参考文档

| 文档 | 内容 |
|---|---|
| `docs/skill/commands.md` | 全部命令的完整说明（原 SKILL.md 命令表原文 + 补齐的命令） |
| `docs/skill/methodology.md` | 分析品种价值、数据源与诚实边界、解读要点、交易哲学、当日决策研判 |
| `docs/skill/account.md` | 实盘持仓评价、实时报价分级、账户快照、consult/serve 接口 |
| `docs/skill/research.md` | 研究纪律、台账、模块地图与待模块化清单 |
| `docs/skill/glossary.md` | 术语表：增仓方向 / 增仓强信号 / 逐行权买卖方 / 墙位等固定名称（对用户说话统一用这些名字） |
| `AGENTS.md` | 改代码的约定：分层、静默失败、统计口径、金融语义、提交流程 |
| `undertow/*/README.md` | 各层逐文件职责与已知边界 |

## 前置
Python 3.9+，零第三方依赖；在仓库根目录运行 `python -m undertow <command>`；长桥走 `longbridge` CLI（只读，经 `undertow.collect.lb_budget` 限额）。
品种 key：交易 `gold silver qqq tqqq`，分析 `wti dxy tlt spy`，观察 `iwm`；`python -m undertow list` 查看。
输出只作波段级风险情景，不是投资建议、不是交易指令。
