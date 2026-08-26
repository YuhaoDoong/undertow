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
公开数据 + 券商（只读）
        ↓
undertow.collect      抓取、规整、缓存、落盘快照
        ↓
undertow.core         公共模型、配置、交易日历、时钟
        ↓
undertow.analyze      确定性指标、信号、情景、实盘持仓评价
        ↓
undertow.report       终端 / JSON / 自包含 HTML
        ↓
undertow.consult      模型无关的上下文包 + 本地只读 HTTP API
undertow.soul         交易者自己的规则、计划与日记（私有）
```

### 模块地图

```text
undertow/
├── core/                    公共模型、配置、时钟、事件日历
│
├── collect/                 ── 数据层（一源一文件）──
│   ├── cftc_cot             CFTC COT 持仓（Disaggregated / Legacy）
│   ├── cboe_options         期权链（ETF 代理 GLD/SLV/USO/QQQ）
│   ├── cboe_history         历史日线 · cboe_vol  GVZ/OVX/VXSLV 波动率指数
│   ├── yahoo_futures        真实期货价（GC=F/SI=F/CL=F/NQ=F）
│   ├── fred_macro           实际利率、美元、通胀预期
│   ├── faireconomy_cal      经济日历 feed（预测/前值/影响级别）
│   ├── longbridge_account   实盘持仓、资产、资金流水、成交            【只读】
│   ├── longbridge_quote     ETF 各场次实时价 + 期权 last/IV           【只读】
│   ├── longbridge_news      品种相关新闻标题                          【只读】
│   └── store · cache        快照仓库（入 git）· TTL 缓存
│
├── analyze/                 ── 确定性分析（无 I/O）──
│   ├── positioning・signals    净头寸、拥挤度、聪明钱背离、
│   │                           逼空蓄势（高集中度净空 × 投机押多）
│   ├── gamma・flow・expiry_ladder   OI 墙、GEX、零伽马；ΔOI × Delta修正ΔIV 判买卖方；
│   │                           逐到期切片
│   ├── macro・volregime・vrp_history   宏观背景、波动率环境、波动率风险溢价
│   ├── outlook・verdict        多因子加权投票 + 近端/中期双周期分层；
│   │                           当日决策研判（做空? 现价追? 短线 长线）
│   ├── fibonacci・risk_reward  摆动腿、黄金回撤、盈亏比闸门
│   ├── technicals              均线结构、RSI/KDJ/MACD/布林 → 短线过热分
│   ├── strategy_hub・strategy・condor・credit_spread   策略情景参数化
│   ├── portfolio               实盘持仓评价：组合期权识别（垂直/跨式/铁鹰/日历）、
│   │                           整品种策略姿态、资金约束、实时价或 BS 估值
│   ├── healthcheck             分级风险体检 + 三套进场闸门
│   │                           （卖方边际 / 买方边际 / 单腿σ与delta）+ 扣费后期望值
│   ├── newsfeed                品种新闻 + 临近高影响事件告警
│   ├── event_impact            数据落地前后的横截面快照与对比
│   └── backtest・blackscholes  无前视事件研究 · 最小 BS 工具
│
├── report/                  markdown · html · viz（纯 SVG，无 JS）
│
├── consult/                 ── AI 接入层 ──
│   ├── packet               确定性上下文包 + 可直接投喂的 prompt
│   └── server               本地只读 HTTP API（无任何写/下单端点）
│
└── soul/                    ── 对象是交易者本人，不是市场（私有数据）──
    ├── profile              铁律、限额、已知弱点、教训、触发计划、待研究问题；
    │                        确定性纪律核查
    ├── plan                 计划交易：触发条件、出场四要素、下单参数
    └── journal              交易日记（含手续费）+ 事前判断打分

config/                      品种、日历、灵魂档案模板
scripts/                     daily_update.sh · event_watch.sh（launchd 定时）
tests/                       28 个无网络单元测试文件
data/snapshots/              期权链历史（入 git —— 不可再生）
data/history/events/         事件影响快照（入 git）
data/account/ · data/soul/   私有：永不入库（已 gitignore）
```

每个包内含 README 说明职责。依赖单向流动：`collect → core → analyze → report/consult/soul`，
分析层从不 import 数据收集层。

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
