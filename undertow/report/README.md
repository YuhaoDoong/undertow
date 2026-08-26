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
