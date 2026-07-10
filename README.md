# undertow — 读懂价格表面之下的机构持仓暗流

机构持仓情报工具，复盘**黄金 / 白银 / 原油 / 美元指数**的大资金结构。**纯标准库 Python，
零依赖**；可作为 **Agent Skill** 装进 Claude Code（`.claude/skills/`）或 Codex（`.codex/skills/`），
见 `SKILL.md`。三层情报 + 回测 + 综合研判：

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
python3 -m undertow list                    # 列出已配置品种（金/银/油/美元，及各自数据层）
# —— COT 持仓层 ——
python3 -m undertow analyze                 # 全部品种
python3 -m undertow analyze gold silver     # 指定品种
python3 -m undertow analyze dxy             # 美元指数（走 Legacy 报告：非商业/商业）
python3 -m undertow analyze --lookback 104  # 自定义历史回看周数
python3 -m undertow analyze gold --json     # 结构化 JSON（喂给上层/LLM）
# —— 期权 Gamma 层 ——
python3 -m undertow gamma                   # 全部品种 Gamma/OI 结构
python3 -m undertow gamma gold --json       # 结构化 JSON
python3 -m undertow gamma --horizon 30      # 近月窗口天数（默认45）
# —— 期权资金流 / 异动层 ——
python3 -m undertow snapshot                # 落盘今日期权链（每天跑一次，攒历史）
python3 -m undertow flow                    # 单快照异常活跃 +（≥2天后）日对日 ΔOI/ΔIV
python3 -m undertow flow gold --json        # 结构化 JSON
python3 -m undertow flow --no-snapshot      # 只用已落盘数据，不自动拉今日
# —— 信号回测层（校准阈值）——
python3 -m undertow backtest                # COT 信号历史前瞻收益
python3 -m undertow backtest gold --json    # 结构化 JSON
python3 -m undertow backtest --horizons 5 10 20  # 前瞻交易日
# —— 事件雷达层 ——
python3 -m undertow calendar                 # 关键节点倒计时 + 本周实时预测/前值/影响（FairEconomy feed）
python3 -m undertow calendar silver --within 40  # 限定品种 + 放宽窗口天数（也自动嵌入 report 顶部）
python3 -m undertow calendar --no-live        # 不拉实时 feed，仅用 config/calendar.json 手维护锚点
# —— 综合研判报告（四层聚合 + 可视化 + 情景）——
python3 -m undertow report                  # 各品种 HTML 研判报告（含 3 张 SVG 图）
python3 -m undertow report gold             # 指定品种
python3 -m undertow report --json           # outlook 结构化 JSON（喂上层/LLM）
# 产出在 data/reports/{品种}_{日期}.html，浏览器打开即看（macOS: open data/reports/...）
```

## 架构（四层 + 公共核心，严格单向依赖，像 skill 一样模块分明）

```
SKILL.md / AGENTS.md        Agent Skill 清单（Claude Code / Codex 通用）
config/instruments.json     品种注册表：加品种 = 改这个 JSON，不改代码
config/calendar.json        关键事件表：FOMC/数据/COT/期权到期（美东日历，手维护，日期须核源）
undertow/
  __main__.py               python -m undertow 入口
  cli.py                    命令编排（analyze/gamma/snapshot/flow/backtest/report/calendar/list）
  core/                     【公共核心】不依赖其它层，可被自由引用
    models.py               数据模型（CotReport / OptionsSnapshot 等，纯数据）
    config.py               读 config/instruments.json、路径
    clock.py                ★ 美东时钟（用户在 SGT，交易日按美东算）
    calendar.py             ★ 事件日历：读 calendar.json + 倒计时/窗口过滤（事件雷达）
  collect/                  【数据收集层】各 API 收敛成语义模型 + 快照仓库 + 缓存
    base.py                 数据源抽象 + 标准库 HTTP 工具
    cftc_cot.py             ★ CFTC COT：Disaggregated(物理品) + Legacy(美元指数等金融品)
    cboe_options.py         ★ CBOE 期权（OCC 解析、OI/gamma/iv）+ 原始落盘 + 内容指纹去重
    cboe_history.py         ★ CBOE 历史日线（回测用，免 key）
    cboe_vol.py             ★ CBOE 波动率指数（GVZ/OVX/VXSLV）
    yahoo_futures.py        ★ 真实期货价 GC=F/SI=F/CL=F/DX=F（urllib 直连，零依赖）
    fred_macro.py           ★ FRED 宏观（实际利率/美元/通胀预期，免 key）
    faireconomy_cal.py      ★ 经济日历实时 feed（FairEconomy 公开 JSON，带预测/前值/影响）
    store.py                ★ 快照仓库：期权链原始 payload 按日 gzip 落盘（永久档案，入 git）
    cache.py                文件缓存（带 TTL，临时、可覆盖）
  analyze/                  【数据分析层】只吃 core.models，与数据源解耦，纯确定性计算
    positioning.py          ★ 净头寸 / 周变化分解 / 历史分位 / z-score
    signals.py              ★ COT 规则解读：拥挤、背离、互换商压力（阈值集中可调）
    blackscholes.py         BS gamma（零伽马翻转位重定价）
    gamma.py                ★ OI 墙 / Put-Call 比 / 做市商 GEX / 零伽马翻转
    flow.py                 ★ 资金流：单快照异动 + 两日 ΔOI/ΔIV 买卖方 + 多腿价差识别 + 波动率面(ATM IV/skew)
    macro.py                ★ 宏观背景：实际利率/美元/通胀预期 + 波动率指数
    backtest.py             ★ 信号事件研究：无前视、发布滞后、对齐收益、分位分桶
    outlook.py              ★ 综合研判：多维因子按可信度加权投票 + 关键位 + 情景
    strategy.py             ★ 策略情景参数化（期货）：方向随研判、位点随结构、缓冲随 ATR、实时层否决票
  report/                   【报告层】只吃 analyze 结果，只管展示
    markdown.py             终端 Markdown 报告（COT / Gamma / 资金流 / 回测）
    html.py                 ★ 自包含 HTML 研判报告（内嵌 SVG，浏览器直接看）
    viz.py                  ★ 手绘 SVG 图（价格+关键位 / OI 墙 / 持仓历史，零依赖）
tests/                      单元测试（不依赖网络，74 个）
data/snapshots/             ★ 期权链每日快照（gzip，入 git = 备份；不可再生）
data/reports/               综合研判 HTML 报告（按品种/日期，入 git；同日重生成自动留档 _rHHMM）
data/cache/                 缓存落盘（自动生成，.gitignore）
```

职责清晰、单向依赖：**collect** 把各家 API 收敛成语义模型；**analyze** 只吃模型做确定性
计算；**report** 只管展示；**core** 是三者共享的地基。任何一层都能单独替换/测试。
**加品种** = 改 JSON；**加数据源** = `collect/` 加一个文件（真·CME 期货期权数据源亦在此
即插即用，分析层零改动）。LLM 不参与算数，只做编排与解读。

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
- **大户集中度**（`ConcentrationStats`，作者口径 R10）：CFTC Concentration Ratios 的前 4/8 大
  净多/净空集中度（占 OI%）+ 周变化 + 历史分位——净空集中度上行 = 空头火力向大户集中
  （金 6/30 实测 52.8%、+1.7pp、156 周分位 89）。展示于 analyze 终端与 report「持仓结构」卡。

**Gamma 层指标**（`analysis/gamma.py`）：
- **OI 墙**：现价 ±15% 内最大 call OI=阻力墙、最大 put OI=支撑墙 → 吸附/pin 候选。**不依赖任何假设，最可靠**。
- **Put/Call OI 比**：情绪/偏度。
- **净 GEX**：做市商伽马敞口正负。负伽马=助涨助跌(放大波动)；正伽马=抑波动/易钉。
  **依赖"做市商净多 call、净空 put"这一行业惯用但不确定的假设**。
- **零伽马翻转位**：用 BS gamma 在不同现价下重定价扫描求得，价格越过它做市商对冲方向反转。

**资金流 / 异动层**（`analysis/flow.py`，复刻文章作者「逐行权价分买卖方」读法）：
- **今日异常活跃**（单快照即可）：近月、现价附近，`volume/OI` 高的行权价。**量 ≫ OI = 多为当日新建仓**，是 ΔOI 异动的**当日先兆**。
- **日对日买卖方判定**（需 ≥2 天快照）：逐 (到期,行权价,C/P) 给出 **ΔOI / 当前OI / 精确Delta / Delta修正ΔIV / 判断**，复刻作者那张表。
  - **核心窍门**：延迟数据无逐笔成交（没有"谁主动"的 tick 标记），买卖方是**推断而非统计**——
    用 **IV 变化方向**作代理。直觉：IV 就是期权的贵贱，**谁急谁付钱**——建仓时权利金被买贵了
    是买方急，被卖便宜了是卖方急。四象限全表：

    | ΔOI | ΔIV | 判定 | 逻辑 |
    |---|---|---|---|
    | 增 | 升 | **买方**（保护/追价） | 新仓建立时权利金被抬高 → 买方主动付价 |
    | 增 | 降 | **卖方**（写权压制/做支撑） | 新仓建立时权利金被压低 → 卖方在供给 |
    | 减 | 升 | **卖方撤退** | 平仓时权利金上涨 → 卖方付钱回补离场（put 端=支撑减弱） |
    | 减 | 降 | **买方了结** | 平仓时权利金下跌 → 买方获利/止损离场（put 端=恐慌退潮） |

  - **两道去噪**（不做会系统性误判）：① **Delta 修正**：先扣 `偏斜斜率 × Δ现价`——现价一动，
    沿偏斜曲线每个行权价的 IV 会机械变化，与买卖无关；② **相对化**：行权价足够多（≥8 个）时
    再减全链修正 ΔIV 的中位数，扣掉"全市场 vol 齐涨齐跌"的平移项。剩下的残差才是**该行权价
    特有的**买卖压力（= 作者表里的"相对 IV 变化"列）。
  - **噪音门槛**：|ΔOI| < 50 手不上表；修正 ΔIV 绝对值 < 0.08pp 判"噪音"不定方向；
    ≥1.0pp 的 call 卖压升格"极强压制"；无昨日 IV 的行只按 OI 方向定性（"新建/减仓"）。
  - 判定档：买方保护/轻微保护、卖方做支撑、卖方撤退、买方了结、（call）极强/卖方/轻微压制、
    买方、噪音——**实测对作者 WTI 6/22 原表 20 行吻合 19 行**（仅 1 行 OI 降但作者按大幅 IV 升
    酌情判为买方）。
  - **叠加静态墙位**：新 OI 堆在 put 墙 + IV 升 = 作者那种自我实现的破位预警。
- **速读"今日持仓异动"段**（`structural_moves`）：自动从 ΔOI 里挑最结构性的动作拼进大白话速读，
  优先级 **墙体滚动**（同类相邻行权价一减一增 = 防线/目标平移，双腿各带买卖方判定，如
  "put 端 3,931(put墙) 减仓、4,040 买方保护——**资本更看跌**"）> 墙上大额增减 > 最大单点建仓；
  净方向结论只由有 IV 信息的腿投票，中性腿弃权。外加资金流净倾向（买卖方加权、已扣价差保护腿）。
- **速读"结构对昨变化"句**（`gamma.structure_delta`）：零伽马位移（含"向现价贴近"提示）+
  墙未移时的**增厚/削弱**（同行权价 OI 对昨差）+ 墙迁移；昨日结构以昨日日期锚定重算，
  免到期时间权重失真。**综合分趋势**：每日 bias_score 落盘 `data/history/outlook_scores.json`，
  速读方向块自动给"较上日 -4.8 → -3.4，看空减弱"式对比。
- **速读"对手盘警示"段**（`counter_signals`，标红）：与综合研判方向**相反**的最强 ΔOI 信号
  （偏空时挑 bullish 异动，反之亦然）+ 策略模块否决票短标签——读报先看反方，
  反向证据增多时方向置信应下调。速读整体分【方向】【关键位】【持仓异动】【对手盘警示】四块。
- ⚠️ 买卖方是 **IV 方向代理**推断、非成交主动性；"撤退/了结"只见平仓、谁先动手是推断；
  边界行（IV 变化贴近噪音线）可能与人工酌情判断不同；价差结构会伪装成方向单（见多腿识别）；
  CBOE 不存期权历史，**必须自己每天 `snapshot` 攒**（launchd 已自动化，见下）。

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

**策略模块**（`analyze/strategy.py`，期货；期权占位）——把研判翻译成【可执行核对的情景参数】：
- **方向先行**：完全由综合研判 bias 决定（中性/分歧 → 不出点位，只监控）；定好方向再定点位。
- **两类情景**：`墙前拒绝/承接`（逆势型，触发区 = 墙前 1×ATR）与 `零伽马翻转追认`（顺结构型，
  日收盘破/站翻转点 + 次日回抽确认）；各带 触发条件 / 参考区 / 失效线 / 目标（配机械盈亏比）/ 当前状态。
- **实时层否决票**：Gamma 环境、现价相对零伽马、波动率面（skew 收敛/买方确认）逐票列出——
  否决 ≥2 票时模块裁决"不开枪，等触发"。
- **ATR 缩放**：缓冲区宽度与仓位提示均按 14 日真实波幅（高低价缺失时退化为收盘波幅）。
- **止盈止损方案**（模板拼句）：止损=失效线（含单位风险的价差与百分比）；止盈=沿结构位
  分批（首目标减半仓、次目标离场，各配 R:R）；首目标达成后止损移至入场参考（保本）。
- **报告呈现为"交易票"子卡**：裁决横幅置顶 → 否决票红色 chips → **结构时间轴图**
  （近 N 日 HLC 日K vs 逐日零伽马/墙折线，用历史快照逐日重算 gamma）→ 每个情景一张票
  （状态 pill / 触发条件 / **价格轨道图**：止损红·入场区蓝带·现价黑点·止盈绿 / 🎯止盈止损行）。
- **跨日窗口状态机**（翻转追认）：用最近 ≤10 个【已完成日收盘】对逐日零伽马的序列判定——
  未触发 / 破位日(T+0，等回抽) / 已破位·等回抽 / **追认成立·持仓管理**（明示"按规则仓位
  应已在场内，不是新开仓点"）/ 破位被收复即重置；窗口首日即破位则标"或更早"（左截尾）。
  墙前拒绝亦附窗口注记（近日曾摸墙被打回）。
- ⚠️ 输出是**给人/顾问参考的规则化框架，不是交易指令**；跨场所执行需自行平移基差。

**方法论手册**：`docs/author_playbook.md` —— 从方法论作者帖子蒸馏的判读规则 R1-R13
（每条注实现落点与状态）；原帖按时间归档于 `docs/author_notes.md` + `docs/screenshot/`，
与我们报告的同向/背离逐条留痕，构成"归档→对照→裁决→蒸馏"的持续校对回路。

### 回测的关键发现（2024-06 → 2026-06，指示性）

- **拥挤反指是分品种/分行情的**：`MM_CROWDED_LONG` 在**均值回归的原油**上 20 日对齐收益 +5.4%、命中 100%(n=7)，
  分位分桶单调下行（越拥挤越跌）；但在**单边走牛的黄金**上完全失效（对齐 −2.6%、命中 12%，分位非单调）。
  → **趋势行情里别拿持仓拥挤当反指**；该信号宜加趋势过滤器。
- `SMART_DIVERGE_BULL`（聪明钱逆势吸筹）在金/银上较稳（命中 67~75%）。
- `SWAP_DIR_*` 等小样本(n<10)信号**别当真**。
- 这正是回测的价值：它把"看起来有理"的规则证伪/证实，告诉你**哪个信号在哪种品种、哪种行情下才可信**。

## 每日自动更新（launchd，零 LLM 参与）

`scripts/daily_update.sh` 由 macOS launchd 每天 **14:05（新加坡/北京时间）** 触发：
快照当日期权链 → 有新数据才出三品种报告 → 自动 commit + push（= GitHub 备份）。

- **快照时窗（硬约束）**：脚本只在 **ET 凌晨 1:00–8:59** 运行——下限等 ET 过零点
  （避免与昨日快照同名覆盖）**且** OCC 隔夜更新完期权 OI（早于它抓到的是
  "今日成交量 + 昨日旧 OI"的脏数据，会毁掉 ΔOI 判读）；上限是美股开盘 9:30 ET
  （之后链变成盘中实时数据，不再是干净的"昨日收盘链"）。错过窗口宁可跳过。
  换算成北京时间：**夏令时 13:00–21:30 / 冬令时 14:00–22:30**，定时点 14:05 两制皆安全。
- 休市日快照内容去重后无新文件 → 不出报告、不提交（避免垃圾报告）。
- 安装：`~/Library/LaunchAgents/com.yuhaodoong.undertow.daily.plist`
  （`launchctl bootstrap gui/$UID <plist>`）；日志 `~/Library/Logs/undertow-daily.log`。

## 测试

```bash
python3 tests/test_positioning.py
python3 tests/test_gamma.py
python3 tests/test_backtest.py
# 或 python3 -m pytest tests/ -q
```

## 作为 Agent Skill 安装

本仓库即一个 **Agent Skill**（根目录 `SKILL.md` 提供清单，Claude Code 与 Codex 通用）：

```bash
# Claude Code
git clone https://github.com/YuhaoDoong/undertow ~/.claude/skills/undertow
# Codex CLI
git clone https://github.com/YuhaoDoong/undertow ~/.codex/skills/undertow
```

装好后，问"黄金现在多空怎么看 / 美元持仓拥挤吗 / 原油期权墙在哪"即会触发，
代理在仓库根目录跑 `python -m undertow <command>` 并把结论解读给你。零依赖、无需 pip。

## 路线图（下一步可加）

1. ~~COT 持仓层~~ ✅ · ~~期权 Gamma 层~~ ✅ · ~~价格对齐 + 回测~~ ✅ · ~~资金流/异动层 + 快照落盘~~ ✅ · ~~综合研判 + 可视化 HTML 报告~~ ✅ · ~~分层重构 + Skill 封装~~ ✅ · ~~美元指数(Legacy COT)~~ ✅
2. **真·CME 期货期权**（最大短板）：现期权层是 ETF 代理（USO≈WTI 弱）。接 **IBKR API** 作为 `collect/` 新数据源，拿逐行权价真实 OI/IV/greeks，gamma/flow/价差识别全部复用——原油可信度直接提升。需券商账户 + CME 行情订阅。
3. ~~**每日攒快照**：定时任务自动跑~~ ✅（launchd 每天 14:05，见上节）
4. **美股个股**：架构已支持无 COT 品种（仅期权+宏观）；接入个股/ETF 期权源后加 config 即可。
5. **按回测调阈值**：依回测结论改 `signals.py`（趋势过滤、按品种分阈值）。
6. **DXY 期权层**：若接到合适的美元期权源（如 UUP 或 IBKR），给美元指数补全 gamma/flow + HTML 报告。
