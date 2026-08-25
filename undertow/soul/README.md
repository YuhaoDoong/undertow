# `soul/` — 交易灵魂层 / Trading Soul

> undertow 里**唯一以「人」为对象**的模块：其它层分析市场，这一层**约束交易者自己**。
>
> The only layer whose subject is the trader, not the market. It records the user's own
> trading system and enforces it deterministically.

## 三件事

1. **沉淀** —— 把与 AI 讨论中确立的交易哲学、铁律、纪律、已知弱点、历史教训，写成**结构化档案**
   （可随复盘持续演进）。
2. **执行** —— 档案里【可机器检查】的限额（单笔风险%、集中度、最低盈亏比、空头腿了结线、
   禁止强平风险结构/二元事件投机）变成对**当前持仓/拟开仓**的确定性核查——**纪律不再靠记忆，靠代码**。
3. **贯通** —— 档案进 `consult` 上下文包并置于 prompt **最前**：任何与你讨论的 AI（本地的我、或你
   接入的其它模型）都**先读到你的规则**，据此给意见；提问里若流露档案记录的弱点（回本心态、追损、
   想一击翻倍），**要求 AI 直接点出来而不是顺着回答**。

## 文件

| 文件 | 作用 |
|---|---|
| `profile.py` | 数据模型（`Rule` / `Weakness` / `Lesson` / `Limits` / `SoulProfile`）+ 读写 + `check_against_profile()` 确定性纪律核查 + 人读渲染。 |

## 入口

- `python -m undertow soul` —— 显示档案（铁律/纪律/限额/弱点/教训）
- `python -m undertow soul --check` —— 用档案的限额**核查当前实盘持仓是否破戒**
- `python -m undertow soul --json` —— 结构化输出
- 档案自动进 `consult` / `consult --pre-trade` 的上下文包

## 档案位置与隐私

档案存 **`data/soul/profile.json`**，含个人交易史与心理弱点 —— **已 gitignore，绝不进公开仓库**
（与 `data/account/` 同等对待）。**模块代码**进公开仓库，**档案内容**只在本机。

## Boundary / 边界

吃 `analyze`（PortfolioReview）与自身档案；不做市场分析、不算行情数值。
档案里的规则是**用户自己确立的纪律**，**不是投资建议**——本模块只负责忠实记录与检查。
