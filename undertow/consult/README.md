# `consult/` — 咨询与开放 AI 接入层 / Consultation & AI Interface

> 把"和 AI 对话复盘 / 开仓前问诊"**接口化、模型无关**。装配一个确定性的「咨询上下文包」，
> 任何 AI（本地的 Claude、或用户接的 GPT/Gemini/本地模型）都能消费来给意见——
> **所有数字都在包里、由上游确定性模块算好，AI 只解读、不臆算**（"LLM 不碰算术"的接口化落地）。
>
> Model-agnostic consultation layer. Assembles a deterministic context packet any AI can consume;
> all numbers come from upstream deterministic modules — the AI only interprets.

## 文件

| 文件 | 作用 |
|---|---|
| `packet.py` | `build_consult_packet(...)`：把品种研判 + 持仓评价 + 🩺体检 + 用户问题（+可选 `--pre-trade` 拟开仓评估）装成 **JSON 可序列化**的包，外加一段渲染好、可直接投喂任意 LLM 的 `prompt`。内置 `GUIDANCE` 硬规则（只读/非投资建议/不臆算/信息不足如实说）。<br>Deterministic packet builder + ready-to-feed prompt. |
| `server.py` | 标准库 `http.server` 起的**本地只读** HTTP API：`GET /consult?q=…`、`/prompt`、`/positions`、`/health`；**POST 一律 405、无任何下单端点**。别的 AI GET 拿确定性上下文包 → 喂给它自己的 LLM。<br>Localhost read-only HTTP API; no write/order endpoints. |

## 入口（CLI）

- `python -m undertow consult "问题"` —— 打印可投喂任意 LLM 的 prompt；`--json` 出机器可读完整包。
- `python -m undertow consult --pre-trade "代码:数量:成本,…"` —— **开仓前问诊**：同一确定性引擎评拟开仓的盈亏比/盈亏平衡/资金/体检。
- `python -m undertow serve [--port 8787]` —— 起本地只读 HTTP API。

## Boundary / 边界

吃 `analyze`（portfolio/healthcheck 结果）与调用方喂入的 `InstrumentContext`；不自己算数值。
**只读**：本层及任何接入的 AI 绝不下单，执行永远由用户在券商端完成。输出只作波段级风险情景参考、
非投资建议。账户数据只在本机流转，**不进公开仓库**。
