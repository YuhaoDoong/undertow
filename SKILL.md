---
name: undertow
description: >-
  读懂价格表面之下的机构持仓暗流。聚合 CFTC COT 持仓、期权 Gamma/OI 墙、买卖方
  资金流（含多腿价差识别）与 FRED 宏观，给出黄金/白银/原油/美元指数的方向研判、
  关键支撑阻力位与情景推演。当用户问期货持仓、期权 OI 墙/Gamma、聪明钱/资金流向、
  COT 报告、或想要这些品种的多空研判与关键位时使用。Use for futures positioning
  (COT), options gamma / OI walls, smart-money options flow, and directional reads
  on gold, silver, WTI crude, and the US Dollar Index.
---

# Undertow — 持仓暗流情报

把价格之下看不见的机构持仓「暗流」量化成可读的研判：谁在加仓、墙在哪里、
买卖方在博弈什么、宏观背景偏哪边。**纯标准库 Python，确定性计算**——LLM 只负责
编排与解读，所有数值走代码，不臆造。

## 何时用
用户问到以下任一，就用本 skill（在仓库根目录跑命令，把输出解读给用户）：
- 「黄金/白银/原油/美元 现在多空怎么看？关键位在哪？」→ `report` 或 `analyze`
- 「COT 持仓 / 聪明钱 / 投机资金拥挤度」→ `analyze`
- 「期权 OI 墙 / Gamma / 零伽马翻转」→ `gamma`
- 「期权资金流 / 谁在买谁在卖 / 价差结构」→ `flow`
- 「这套信号历史上准不准」→ `backtest`
- 「最近有什么大事件 / FOMC/CPI/非农 什么时候 / 临近哪些催化剂」→ `calendar`

## 前置
- Python 3.9+；**无需 pip 安装任何依赖**（仅标准库 urllib/json/statistics…）。
- 所有命令在**仓库根目录**运行：`python -m undertow <command> [品种...]`。
- 品种 key：`gold` `silver` `wti` `dxy`（`python -m undertow list` 查看各品种已配齐哪些层）。

## 命令
| 命令 | 作用 | 产物 |
|---|---|---|
| `python -m undertow report [品种...]` | 四层聚合综合研判 + SVG 图 | 自包含 HTML 落在 `data/reports/`（用真实期货价/位点） |
| `python -m undertow analyze [品种...]` | COT 持仓分析（净头寸/拥挤度/聪明钱背离） | 终端 Markdown；`--json` 给结构化 |
| `python -m undertow gamma [品种...]` | 期权 Gamma/OI 墙/零伽马 | 终端 Markdown |
| `python -m undertow flow [品种...]` | 买卖方资金流 + 多腿价差识别 | 终端 Markdown（需≥2 天快照） |
| `python -m undertow backtest [品种...]` | COT 信号事件研究回测 | 终端 Markdown |
| `python -m undertow snapshot [品种...]` | 落盘当日期权链原始全字段 | gzip 存 `data/snapshots/`（纳入 git） |
| `python -m undertow calendar [品种...]` | 事件雷达：关键节点倒计时 + **实时预测/前值/影响**（本周自动拉 FairEconomy 公开 feed，远期用手维护锚点） | 终端；也自动嵌入 `report` 顶部。`--no-live` 仅用本地锚点 |
| `python -m undertow list` | 列出品种与各自数据层 | 终端 |

留空品种 = 全部。全局开关：`--no-cache`（绕过缓存）置于子命令前，如 `python -m undertow --no-cache analyze gold`。

## 架构（四层 + 公共核心，改一层不动其它）
```
undertow/
  core/     models / config / clock          公共核心
  collect/  各数据源 + 快照仓库 + 缓存          数据收集层
  analyze/  positioning/gamma/flow/macro/      数据分析层（只吃 core.models）
            outlook/backtest/signals/blackscholes
  report/   markdown / html / viz             报告层
  cli.py + __main__.py
```
- **新增品种** = 改 `config/instruments.json`，不动代码。
- **新增数据源** = `collect/` 加一个文件（真·CME 期货期权数据源也在此即插即用，分析层零改动）。

## 数据源与诚实边界（务必如实转达用户）
- **持仓 COT**：CFTC 官方（免费）。物理商品走 Disaggregated，金融期货（美元指数）走 Legacy。✅ 真实，周频、滞后约 3 天。
- **期权链**：CBOE 延迟报价，**用 ETF 代理**（GLD≈金 好 / SLV≈银 好 / USO≈油 **弱**）。黄金可信，原油位点仅定性。**这不是作者看的真·CME 期货期权**——后者需券商/付费数据源（如 IBKR）才能拿到。
- **价格/位点**：最终位点用**真实期货价**（Yahoo GC=F/SI=F/CL=F/DX=F）+ 当日实时比值换算，免乘数漂移。
- **宏观**：FRED（真实利率/美元/通胀预期）。**波动率**：CBOE GVZ/OVX/VXSLV。
- 美元指数（dxy）暂无合适免费期权代理，只走持仓+价格+宏观，用 `analyze dxy`（不出 HTML 报告）。
- **事件日历**：远期锚点（FOMC 全年/COT/OPEX）手维护于 `config/calendar.json`（日期须核官方源）；本周的数据预测/前值/影响自动拉 **FairEconomy 公开 JSON feed**（ForexFactory 数据方，合法消费公开 feed、非爬网页，标注 (FF)）。**不爬 ForexFactory/Investing 网页**（Cloudflare 反爬 + ToS）。
- 全部只作**波段级风险情境**预警，不构成交易指令；不绕过任何数据源的反爬/ToS。

## 日常习惯（重要）
**CBOE 不提供期权历史**——资金流的日对日 diff 只能靠自己每天攒。每个**美东交易日**跑一次：
```
python -m undertow snapshot      # 落盘当日全品种期权链；休市重复数据会自动跳过
```
快照 gzip 后纳入 git，push 即等于备份这份不可再生的历史。

## 解读要点
- `report` 的 HTML 自带表格/图/情景，直接把结论（bias + 可信度 + 关键位）讲给用户，并带上**作者口径的买卖方表**与**价差结构**警示。
- 始终区分：**真实数据**（COT/真期货价/宏观）vs **代理近似**（ETF 期权位点）。原油务必声明 USO 代理弱。
