[English](README.md) · **中文**

# undertow

undertow 是一个零运行时依赖的 Python 市场研究工具，用于分析黄金、白银、WTI 原油和美元价格表面之下的机构持仓与衍生品结构。项目整合 CFTC、CBOE 等公开数据、期货与宏观背景、确定性分析、历史事件研究，以及可独立打开的 HTML 报告。

> **仅供研究使用。** undertow 输出的是结构化市场情景，不是价格预测或交易指令。结果依赖延迟公开数据、代理品种、模型假设和启发式分类，必须结合独立验证和风险管理使用。

## 能做什么

| 层次 | 目的 | 主要输出 |
|---|---|---|
| 持仓结构 | 还原 CFTC 参与者结构与周度变化 | 净持仓、拥挤度、资金流质量、集中度和背离 |
| 期权结构 | 分析 CBOE 延迟 ETF 期权链 | OI 墙、Put/Call 比、估算 GEX、零伽马位置 |
| 期权资金流 | 比较快照并识别异常活跃 | Volume/OI、OI 与 IV 变化、推断的增建/减仓压力 |
| 宏观与事件 | 加入利率、美元、通胀、波动率和日历背景 | 宏观环境、事件窗口、预测值/前值/影响信息 |
| 历史验证 | 在避免前视偏差的前提下评估信号行为 | 前瞻收益事件研究和置信度估计 |
| 综合报告 | 把各层整合为可审计的情景 | Markdown/JSON 与含 SVG 图的自包含 HTML |

如果接入 LLM，模型只负责命令编排和结构化结果解释；计算仍由确定性的 Python 代码完成。

## 设计目标

- **零运行时依赖**：应用本身只使用 Python 标准库。
- **公开数据优先**：核心流程使用官方或公开端点，无需 API key。
- **假设透明**：明确说明代理换算、数据滞后、GEX 符号约定和资金流推断。
- **模块清晰**：数据收集、分析与报告保持单向依赖。
- **适合 Agent 调用**：`SKILL.md` 说明如何把命令行接口提供给 Codex 或 Claude Code。

## 快速开始

```bash
git clone https://github.com/YuhaoDoong/undertow.git
cd undertow

python3 -m undertow list
python3 -m undertow analyze gold --json
python3 -m undertow gamma gold --json
python3 -m undertow snapshot gold
python3 -m undertow flow gold --json
python3 -m undertow report gold
```

报告写入 `data/reports/`。HTML 文件为自包含格式，可直接在浏览器打开。

### 常用命令

```bash
# 持仓结构
python3 -m undertow analyze
python3 -m undertow analyze gold silver --lookback 104

# 期权结构与资金流
python3 -m undertow gamma gold --horizon 30
python3 -m undertow snapshot
python3 -m undertow flow --no-snapshot
python3 -m undertow expiry silver

# 宏观、事件与波动率
python3 -m undertow calendar gold --within 30
python3 -m undertow vol gold

# 历史验证与综合报告
python3 -m undertow backtest gold --horizons 5 10 20
python3 -m undertow report gold --json
```

当前参数以 `python3 -m undertow --help` 或子命令的 `--help` 为准。

## 支持品种

| Key | 市场 | 持仓来源 | 期权代理 | 期货参照 |
|---|---|---|---|---|
| `gold` | 黄金 | CFTC Disaggregated COT | GLD | GC=F |
| `silver` | 白银 | CFTC Disaggregated COT | SLV | SI=F |
| `wti` | WTI 原油 | CFTC Disaggregated COT | USO | CL=F |
| `dxy` | 美元指数 | CFTC Legacy COT | — | DX=F |
| `qqq` | 纳斯达克 100 代理 | CFTC Legacy COT | QQQ | NQ=F |

品种定义位于 `config/instruments.json`。通常新增品种只需补配置和兼容的数据收集器，不应改动整个分析层。

## 系统架构

```text
公开数据源
    ↓
undertow.collect      拉取、标准化、缓存并保存快照
    ↓
undertow.core         共享模型、配置、时钟与日历
    ↓
undertow.analyze      确定性指标、信号、回测与情景
    ↓
undertow.report       终端、JSON 和自包含 HTML 展示
```

仓库结构：

```text
undertow/
  core/               领域模型与共享配置
  collect/            CFTC、CBOE、Yahoo、FRED 和日历收集器
  analyze/            持仓、期权、宏观、回测和策略逻辑
  report/             Markdown、HTML 与 SVG 报告
config/                品种和事件配置
tests/                 无网络依赖的单元测试
data/snapshots/        本地累积的期权链历史
data/reports/          生成的报告和归档
SKILL.md               Agent 集成说明
```

各子包还包含简短 README，说明该层的职责和文件。

## 数据来源

| 数据 | 来源 | 主要局限 |
|---|---|---|
| COT 持仓 | CFTC 公开报告 | 周频；周五发布、对应周二持仓 |
| ETF 期权 | CBOE 延迟报价端点 | 延迟且为代理品种；无完整历史期权链 |
| 期货参照 | Yahoo Finance chart 端点 | 可用性和格式不受项目控制 |
| 宏观序列 | FRED CSV 端点 | 日频或更低；历史值可能修订 |
| 波动率指数 | CBOE | 不同序列为延迟或日终数据 |
| 经济日历 | FairEconomy 公开 feed 与本地锚点 | 事件信息可能变化，重要日期需核源 |

## 验证

当前离线测试包含 88 个用例，覆盖解析、时区与日期对齐、到期处理、持仓分析、Gamma、资金流、回测、情景逻辑和报告生成。

```bash
python3 -m pytest -q
```

应用运行不需要第三方库；只有开发测试需要 `pytest`。

## 关键假设与局限

- COT 是慢频数据，且发布时已有数日滞后，不适合日内择时。
- GLD、SLV 和 USO 期权只是商品衍生品代理。ETF 行权价换算为期货价格只能近似，USO 与 WTI 尤其需要谨慎。
- 估算 GEX 依赖做市商净持仓方向假设，真实库存不可见。
- 资金流方向根据 OI 和相对 IV 变化推断，并非逐笔成交的买卖方识别。
- 需要每天运行 `snapshot` 才能在本地累积期权链历史；公开延迟端点不提供完整历史链。
- 回测和置信度只描述已有样本，在新的市场状态下可能失效。

## Agent 集成

安装和命令路由见 [`SKILL.md`](SKILL.md)。Agent 工作流建议使用 `--json`，让模型解释明确字段，而不是从自然语言报告中抓取数值。

## 项目状态

undertow 是持续迭代的研究项目。数据端点和市场惯例可能变化；使用新版本前应核对数据来源并重新运行离线测试。
