# Ask Codex Input

## Question

请对 /Users/yhdong/Trading 的 undertow 项目做一次全面 code review。这是一个纯标准库 Python（零第三方依赖，刻意约束）的商品期货持仓/期权结构情报工具：4 层架构（core/collect/analyze/report），每天自动抓 CFTC COT、CBOE 期权链（ETF 代理）、Yahoo 期货价、FRED 宏观，生成自包含 HTML 研判报告（data/reports/）。75 个单元测试在 tests/。请重点审查：1) analyze/ 层的正确性风险（flow.py 的买卖方判定/价差识别/相对IV去噪、gamma.py 的零伽马扫描、strategy.py 的窗口状态机）；2) cli.py cmd_report 的复杂度（函数过长）与失败模式；3) report/html.py 速读分块与策略卡的信息架构——报告内容还有什么可优化（可读性/信息密度/缺失要素）；4) 任何数据质量陷阱（我们已踩过：到期滚落污染、Yahoo 日线缺根、比值时点失真）。请给出：按严重度排序的问题清单（正确性 bug 优先）+ 报告内容的具体优化建议清单。不要改代码，只输出审查报告。

## Configuration

- Model: gpt-5.5
- Effort: high
- Timeout: 3600s
- Timestamp: 2026-07-10_14-09-27
- Tool: codex
