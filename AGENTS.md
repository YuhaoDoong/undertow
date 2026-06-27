# AGENTS.md — undertow

机构持仓暗流情报工具。纯标准库 Python，确定性计算；本文件给在本仓库工作的 AI 代理
（Codex / Claude Code 等）一份一致的上手说明。技能化用法另见 `SKILL.md`。

## 跑起来
- Python 3.9+，**无第三方依赖**。在仓库根目录运行：`python -m undertow <command> [品种...]`。
- 品种：`gold` `silver` `wti` `dxy`。命令：`report / analyze / gamma / flow / backtest / snapshot / list`。
- 测试：`python -m pytest -q`（应全绿）。

## 代码地图（四层 + core，严格单向依赖）
- `undertow/core/` — models / config / clock，不依赖其它层。
- `undertow/collect/` — 数据源适配器 + 快照仓库 + 缓存（取数都在这）。
- `undertow/analyze/` — 纯计算分析（只吃 `core.models`，不取数）。
- `undertow/report/` — markdown / html / viz 渲染（只吃 analyze 结果）。
- `undertow/cli.py` — 命令编排。`config/instruments.json` — 品种注册表。

## 约定
- **不引入第三方依赖**（零依赖是这个项目的身份；可视化用手写 SVG，不用 matplotlib）。
- 数值一律走确定性代码，LLM 不负责算数；新增计算配单元测试。
- **不绕过任何数据源的反爬/ToS**（尤其 CME 403）；只用可合法访问的 CFTC/CBOE/Yahoo/FRED。
- 新增品种改 JSON 即可；新增数据源在 `collect/` 加文件，分析层不动。
- 提交信息讲清「为什么」；快照（`data/snapshots/`）纳入 git 作为不可再生历史的备份。

## 诚实边界（改动/汇报时保持）
- 期权层是 **ETF 代理**（USO≈WTI 弱）；真·CME 期货期权需券商/付费源（如 IBKR），尚未接入。
- COT 周频滞后约 3 天；结论只作波段级风险情境，不构成交易指令。
