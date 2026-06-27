# trading_intel — 期货/期权持仓情报地基

三层情报 + 回测，复盘黄金 / 白银 / 原油的大资金结构：

1. **COT 持仓层**（CFTC 周报）：投机资金/聪明钱/互换商的多空结构与变化，量化"拥挤度、空头回补 vs 主动建仓、聪明钱背离、互换商方向压力"。
2. **期权 Gamma 层**（CBOE 延迟数据）：按行权价的 OI 墙（吸附/pin 位）、Put/Call 比、做市商 GEX 正负、零伽马翻转位——即文章里最有价值的"关键位点"。
3. **期权资金流 / 异动层**（自落盘快照）：复刻文章作者 6/24 实战那套——**临近到期大单异动**。① 单快照按 `volume/OI` 找"今日异常活跃"（不需历史，当天就能用）；② 两日快照 diff 出 **ΔOI / ΔIV**，分类看跌/看涨增建 vs 减仓，叠加静态墙位。

上面三层 + COT 信号回测，最后汇入一个 **综合研判层（report）**：把各因子按【回测校准的可信度】加权投票出方向倾向，汇总**关键位点**（墙/零伽马/资金流活跃价，换算商品价），给出**规则化情景 + 失效位**，并配**三张手绘 SVG 图**（价格+关键位 / OI 墙 / 持仓历史），输出一个**自包含 HTML 报告**（浏览器直接看）。

> ⚠️ **定位**：这是**波段级的风险情境工具**，不是涨跌预言机。
> COT 滞后约 3 天，不适合日内；投机资金极端持仓常作【反指】；互换商方向含 OTC 对冲歧义。
> 期权层用 **ETF 期权代理**（见下），位点换算商品仅近似；GEX 正负依赖做市商持仓假设。
> 资金流层无逐笔成交，方向为**启发式推断**；ΔOI/ΔIV 需自己每天 `snapshot` 攒（CBOE 不存期权历史）。
> 务必与价格行为多因子共振后再决策。

## 快速开始

无需安装第三方库（纯标准库）。在本目录运行：

```bash
python3 -m trading_intel.cli list                    # 列出已配置品种
# —— COT 持仓层 ——
python3 -m trading_intel.cli analyze                 # 全部品种（金/银/油）
python3 -m trading_intel.cli analyze gold silver     # 指定品种
python3 -m trading_intel.cli analyze --lookback 104  # 自定义历史回看周数
python3 -m trading_intel.cli analyze gold --json     # 结构化 JSON（喂给上层/LLM）
# —— 期权 Gamma 层 ——
python3 -m trading_intel.cli gamma                   # 全部品种 Gamma/OI 结构
python3 -m trading_intel.cli gamma gold --json       # 结构化 JSON
python3 -m trading_intel.cli gamma --horizon 30      # 近月窗口天数（默认45）
# —— 期权资金流 / 异动层 ——
python3 -m trading_intel.cli snapshot                # 落盘今日期权链（每天跑一次，攒历史）
python3 -m trading_intel.cli flow                    # 单快照异常活跃 +（≥2天后）日对日 ΔOI/ΔIV
python3 -m trading_intel.cli flow gold --json        # 结构化 JSON
python3 -m trading_intel.cli flow --no-snapshot      # 只用已落盘数据，不自动拉今日
# —— 信号回测层（校准阈值）——
python3 -m trading_intel.cli backtest                # COT 信号历史前瞻收益
python3 -m trading_intel.cli backtest gold --json    # 结构化 JSON
python3 -m trading_intel.cli backtest --horizons 5 10 20  # 前瞻交易日
# —— 综合研判报告（四层聚合 + 可视化 + 情景）——
python3 -m trading_intel.cli report                  # 各品种 HTML 研判报告（含 3 张 SVG 图）
python3 -m trading_intel.cli report gold             # 指定品种
python3 -m trading_intel.cli report --json           # outlook 结构化 JSON（喂上层/LLM）
# 产出在 data/reports/{品种}_{日期}.html，浏览器打开即看（macOS: open data/reports/...）
```

## 架构（分层解耦，便于调整 / 加功能）

```
config/instruments.json     品种注册表：品种=改这个 JSON，不改代码
trading_intel/
  config.py                 读配置、路径
  models.py                 数据模型（CotReport / OptionsSnapshot 等，纯数据）
  cache.py                  文件缓存（COT 6h / 期权 30min TTL，临时、可覆盖）
  store.py                  ★ 快照仓库：期权链原始 payload 按日 gzip 落盘（永久档案，入 git）
  datasources/
    base.py                 数据源抽象 + 标准库 HTTP 工具
    cftc_cot.py             ★ CFTC COT 数据源（Socrata 72hh-3qpy）+ 字段映射
    cboe_options.py         ★ CBOE 期权数据源（OCC 代码解析、OI/gamma/iv）+ 原始落盘
    cboe_history.py         ★ CBOE 历史日线（GLD/SLV/USO，回测用，免 key）
  analysis/
    positioning.py          ★ 净头寸 / 周变化分解 / 历史分位 / z-score
    signals.py              ★ COT 规则解读：拥挤、背离、互换商压力（阈值集中可调）
    blackscholes.py         BS gamma（用于零伽马翻转位重定价）
    gamma.py                ★ OI 墙 / Put-Call 比 / 做市商 GEX / 零伽马翻转
    flow.py                 ★ 资金流异动：单快照 volume/OI 异常 + 两日 ΔOI/ΔIV diff
    backtest.py             ★ 信号事件研究：无前视、发布滞后、对齐收益、分位分桶
    outlook.py              ★ 综合研判：五维因子(COT/Gamma/Flow/Macro/...)按可信度加权投票 + 关键位 + 情景
    macro.py                ★ 宏观背景：实际利率/美元/通胀预期 → 金银利多利空
  datasources/yahoo_futures.py ★ 真实期货价 GC=F/SI=F/CL=F（urllib 直连，零依赖）
  datasources/fred_macro.py    ★ FRED 宏观（DFII10 实际利率/DTWEXBGS 美元/T10YIE，免 key）
  clock.py                  ★ 统一时钟：以【美东时间】为基准（用户在 SGT，交易日按美东算）
  viz.py                    ★ 手绘 SVG 图（价格+关键位 / OI 墙 / 持仓历史，零依赖）
  report.py                 渲染 Markdown 报告（COT / Gamma / 资金流 / 回测）
  report_html.py            ★ 组装自包含 HTML 研判报告（内嵌 SVG，浏览器直接看）
  cli.py                    命令行入口（analyze/gamma/snapshot/flow/backtest/report/list）
tests/                      单元测试（不依赖网络，30 个）
data/cache/                 缓存落盘（自动生成，.gitignore）
data/snapshots/             ★ 期权链每日快照（gzip，入 git = 备份；不可再生）
data/reports/               综合研判 HTML 报告（按品种/日期，入 git）
```

三层职责清晰：**数据源**只管把各家 API 收敛成语义模型；**分析层**只吃模型做确定性计算；
**渲染层**只管展示。任何一层都能单独替换/测试。LLM 不参与算数，只做编排与解读。

## 数据来源（含踩坑记录）

| 数据 | 来源 | 成本 | 滞后 |
|---|---|---|---|
| COT 持仓 | CFTC publicreporting.cftc.gov（Socrata `72hh-3qpy` Disaggregated 周报） | 免费/官方 | 周五发布，截止当周二 |
| 期权 OI / Gamma | CBOE `cdn.cboe.com/api/global/delayed_quotes/options`（**ETF 代理** GLD/SLV/USO） | 免费/合法 | 延迟，日内更新 |
| 历史日线（回测） | CBOE `cdn.cboe.com/api/global/delayed_quotes/charts/historical`（GLD/SLV/USO） | 免费/合法 | 日终 |
| **真实期货价** | Yahoo `query1.finance.yahoo.com/v8/finance/chart`（**GC=F/SI=F/CL=F**，COMEX/NYMEX 期货） | 免费/合法 | 准实时 |
| **宏观背景** | FRED `fredgraph.csv`（**DFII10** 实际利率 / **DTWEXBGS** 美元 / **T10YIE** 通胀预期） | 免费/官方/免key | 日频 T+1 |

> ⏱ **时钟以美东为准**（`clock.py`）：盯的是美国市场，交易日按 America/New_York 算。
> 用户在新加坡(SGT)，本机日期比美东快约半天到一天，故快照/报告的"今天"统一锚定美东，避免与真实交易日错位。

> 本机实测：CME（403 禁抓，不绕）不可用；**Yahoo chart 接口可达**（urllib 直连，yfinance 底层同款，零依赖）；
> CFTC + CBOE + Yahoo 三个 host 稳定可达。
>
> **最终位点落在真实商品价**：期权链是 ETF 的（位点本以 ETF 计），报告用【当日实时比值】=真实期货价/ETF价
> 换算所有墙位/零伽马到真实金银油价——**免静态乘数漂移**（实测金的真实比值 ≈10.97，非旧的 10.8）；
> 价格图也直接画 GC=F/SI=F/CL=F。**WTI 注意**：USO 与 WTI 价格量级不同（USO≈109 vs WTI≈70）且非线性，
> 真实油价用 CL=F 显示，但 USO 期权位点换算 WTI 仅当日近似、漂移大。

**为什么期权用 ETF 代理而不是 COMEX 原表**：
- CME 对脚本访问**硬封锁**（403 + 明确援引 Data Terms of Use 禁止自动抓取）；不绕过。
- Yahoo 期权接口已改 crumb 鉴权且对本机限流，不稳定。
- CBOE 延迟报价是**合法公开**接口，且**每个行权价直接带 gamma/delta/iv**，免自建定价。
- GLD/SLV/USO 是业界常用的商品期权**代理**：合法、可脚本化。代价是它**不是**文章读的 COMEX 原表——
  位点以 ETF 计，×乘数≈商品价仅近似（**USO 与 WTI 非线性，乘数无效，仅定性**）。
- 若要 COMEX 原表，需付费源（Barchart/CME DataMine）或 QuikStrike 登录手动导出；
  届时只需新增一个 `datasources/*.py`，**分析层不变**。乘数在 `config/instruments.json` 可校准。

合约代码（已校验）：黄金 `088691`、白银 `084691`、WTI 原油 `067651`。

## 信号 / 指标说明

**COT 层规则**（阈值集中在 `analysis/signals.py` 顶部，回测后可调）：
- **MM_CROWDED_LONG / SHORT**：投机资金净头寸处历史分位极值 → 拥挤，作**反指**看回调/挤空。
- **MM_FLOW_QUALITY**：本周净变化来源分解——主动建仓(强) vs 空头回补/多头了结(弱)。
- **SMART_DIVERGE_***：聪明钱(Other Reportables)与投机资金背离（防守/吸筹）。
- **SWAP_DIR_***：互换商方向性压力（复刻文章逻辑，强标 OTC 对冲歧义，仅作辅助）。

**Gamma 层指标**（`analysis/gamma.py`）：
- **OI 墙**：现价 ±15% 内最大 call OI=阻力墙、最大 put OI=支撑墙 → 吸附/pin 候选。**不依赖任何假设，最可靠**。
- **Put/Call OI 比**：情绪/偏度。
- **净 GEX**：做市商伽马敞口正负。负伽马=助涨助跌(放大波动)；正伽马=抑波动/易钉。
  **依赖"做市商净多 call、净空 put"这一行业惯用但不确定的假设**。
- **零伽马翻转位**：用 BS gamma 在不同现价下重定价扫描求得，价格越过它做市商对冲方向反转。

**资金流 / 异动层**（`analysis/flow.py`，复刻文章作者「逐行权价分买卖方」读法）：
- **今日异常活跃**（单快照即可）：近月、现价附近，`volume/OI` 高的行权价。**量 ≫ OI = 多为当日新建仓**，是 ΔOI 异动的**当日先兆**。
- **日对日买卖方判定**（需 ≥2 天快照）：逐 (到期,行权价,C/P) 给出 **ΔOI / 当前OI / 精确Delta / Delta修正ΔIV / 判断**，复刻作者那张表。
  - **核心窍门**：延迟数据无逐笔成交，但用 **IV 变化方向**作买卖方代理——**OI增+IV升=买方抬价**（看跌买保护 / 看涨买突破）；**OI增+IV降=卖方写权**（写put做支撑 / 写call做压制）。
  - **Delta 修正ΔIV** = 剔除「现价移动沿偏斜的机械 IV 变化」后的残差（对作者方法的原理化近似）。
  - 判定档：买方保护/轻微保护、卖方做支撑、卖方撤退、（call）极强/卖方/轻微压制、买方、噪音——**实测对作者 WTI 6/22 原表 20 行吻合 19 行**（仅 1 行 OI 降但作者按大幅 IV 升酌情判为买方）。
  - **叠加静态墙位**：新 OI 堆在 put 墙 + IV 升 = 作者那种自我实现的破位预警。
- ⚠️ 买卖方是 **IV 方向代理**推断、非成交主动性；Delta 修正是近似、边界行可能与人工酌情判断不同；CBOE 不存期权历史，**必须自己每天 `snapshot` 攒**。

**回测层**（`analysis/backtest.py`）——用历史价格校准上面的信号/阈值：
- 无前视逐周重算信号；入场按 COT 周五发布滞后；前瞻 5/10/20 交易日收益。
- **对齐收益**=顺信号方向交易的收益；要显著为正、命中率>50%、且优于「无条件基线」才算有效。
- **MM 净分位分桶**：直接看"越拥挤→前瞻收益越低"是否成立，校准拥挤阈值。

**综合研判层**（`analysis/outlook.py` + `viz.py` + `report_html.py`）——把四层拢成一份可视化报告：
- **方向倾向**：各因子（COT 信号 / 墙位空间 / P-C 比 / 资金流）按【回测校准的可信度】加权投票，得 偏多/偏空/中性/分歧 + 综合分 + 可信度。**权重与依据逐条列出、可审计**；拥挤反指等已知不可靠的信号被降权。
- **关键位点**：墙 / 零伽马 / 近到期 pin / 资金流活跃价，统一换算到商品价。
- **情景推演**：规则化 if-then（守 X 则区间 / 破 Y 则趋势放大），**含失效位**——给的是"该盯哪些位、什么情况证伪"，**不是点位预言**。
- **可视化**：3 张手绘 SVG（价格+关键位横线 / OI 墙发散条形 / 投机资金净持仓历史），内嵌进自包含 HTML。**纯标准库、零依赖**。
- ⚠️ "预测"是确定性规则聚合，不是预言机；LLM 不参与算数。务必与价格行为共振后决策。

### 回测的关键发现（2024-06 → 2026-06，指示性）

- **拥挤反指是分品种/分行情的**：`MM_CROWDED_LONG` 在**均值回归的原油**上 20 日对齐收益 +5.4%、命中 100%(n=7)，
  分位分桶单调下行（越拥挤越跌）；但在**单边走牛的黄金**上完全失效（对齐 −2.6%、命中 12%，分位非单调）。
  → **趋势行情里别拿持仓拥挤当反指**；该信号宜加趋势过滤器。
- `SMART_DIVERGE_BULL`（聪明钱逆势吸筹）在金/银上较稳（命中 67~75%）。
- `SWAP_DIR_*` 等小样本(n<10)信号**别当真**。
- 这正是回测的价值：它把"看起来有理"的规则证伪/证实，告诉你**哪个信号在哪种品种、哪种行情下才可信**。

## 测试

```bash
python3 tests/test_positioning.py
python3 tests/test_gamma.py
python3 tests/test_backtest.py
# 或 python3 -m pytest tests/ -q
```

## 路线图（下一步可加）

1. ~~COT 持仓层~~ ✅ · ~~期权 Gamma 层~~ ✅ · ~~价格对齐 + 回测~~ ✅ · ~~资金流/异动层 + 快照落盘~~ ✅ · ~~综合研判 + 可视化 HTML 报告~~ ✅
2. **每日攒快照**：把 `snapshot` 设成每日定时任务（cron），让 `flow` 的 ΔOI/ΔIV 持续有料；快照入 git 即备份。
3. **按回测调阈值**：依回测结论改 `signals.py`（如给 `MM_CROWDED_*` 加趋势过滤、按品种分阈值）。
4. **Gamma/Flow 历史回测**：快照攒够后，把 flow 异动信号也纳入事件研究（验证"近月大单异动"的前瞻收益）。
5. **COMEX 原表（可选）**：接付费源拿真实 COMEX 期权 OI，与 ETF 代理交叉验证。
6. **封装 skill**：把"拉数→分析→出报告"包成一个 Claude Code skill，按需触发。
