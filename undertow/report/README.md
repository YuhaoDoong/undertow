# `report/` — 报告层 / Presentation

> Layer 3. Eats `analyze` results and **only renders** — no computation, no I/O beyond returning strings. Swap the renderer without touching any analysis.
>
> 第 3 层。只吃 `analyze` 的结果**做展示**——不算数、不碰网络（只返回字符串）。换渲染器不动分析层。

## Files / 文件

| 文件 | 作用 |
|---|---|
| `markdown.py` | 终端 Markdown 报告：COT / Gamma / 资金流 / 回测 / 近周到期阶梯。<br>Terminal Markdown for each CLI command. |
| `html.py` | **自包含 HTML 研判报告**（内嵌 SVG，浏览器直接看）：综合研判 + 速读 + 关键位 + **技术面超买超卖** + 资金流 + 到期阶梯 + 策略票 + 情景。<br>技术面卡片以 `analyze/stretch.py` 的拉伸度为主、传统过热分为辅，每个非中性档强制带上回测边缘/胜率/n/Welch t 与显著性判定——**不输出未校准的裸标签**；两者分歧时显式告警并说明机理（RSI/KDJ/CCI 测"走得多急"、拉伸度测"离常态多远"）。<br>Self-contained HTML report with inline SVG. |
| `viz.py` | 手绘 SVG 图表（**纯标准库零依赖**）：价格+关键位 / OI 墙发散条形 / 持仓净额历史 / 结构时间轴 / 价格轨道 / 波动率曲线。<br>Hand-rolled SVG charts, stdlib only. |

## Design notes / 要点

- **零依赖**：SVG 全靠字符串拼，不用 matplotlib/plotly；HTML 自包含，无外链、无 JS 框架。<br>Zero deps — SVG is string-built, HTML is fully inlined.
- **不算数**：所有数值来自 `analyze`；本层只格式化。始终区分**真实数据**（COT/真期货价/宏观）vs **代理近似**（ETF 期权位点），并在文案里标注。<br>No arithmetic here; always labels real-data vs ETF-proxy.

## Boundary / 边界

imports `core` + `analyze` 的结果类型；**不 import** `collect`，**不**反向被 `analyze` 引用。

## 研报 v2（`v2.py`，2026-09-30 起）

用户：「你新建个研报体系吧，先只放期权墙。后面我们证实哪些信号有用，就放什么上去。之前的研报也照常出。」
随后：「之前的期权关键点位，墙位历史可视化。然后方向判断也先放上去，注明还在验证截断。这样一个完整的期权结构。
同样也是一个index 界面，然后点击品种进入详细界面」

- 命令 `undertow report-v2 [品种…]` → `data/reports/v2/v2_<日>.html`（首页）+ `v2_<日>_<品种>.html`（详情）+ `.md`。
  daily 在旧研报之后生成：快照未到 rc=3（不告警），计算失败 rc=1（告警），都不阻断。
- 详情页：① 期权墙总览（按到期）② 按到期分层 ③ 关键点位 + 价格图 + 近价 OI 分布 ④ 墙位历史 ⑤ 方向判断（验证中）。
  ②③④ 复用旧研报同一批 analyze 函数与渲染（不另立口径）；只读已落盘的认证快照，不抓链。
- **准入规则写死在 `SECTIONS` + `section_allowed()`**：
  - observation：持仓结构、数据事实，写明口径即可上；
  - validating：方向判断仍在前瞻预登记检验中 —— 只有 claims 里理由为 prospective_study、且有冻结规则版本的主张可展示，醒目标注「验证中」、不进结论；
  - prediction：已通过检验，必须 T1（目前没有）。
- 以后新增栏目：在 claims 里登记，走完预登记检验达到 T1（或作为验证中的前瞻研究），再在 `SECTIONS` 加一行。
