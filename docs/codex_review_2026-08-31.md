Reading additional input from stdin...
OpenAI Codex v0.151.0
--------
workdir: /Users/yhdong/Trading
model: gpt-5.6-sol
provider: openai
approval: never
sandbox: read-only
reasoning effort: medium
reasoning summaries: none
session id: 01a05b65-6fb8-7711-8a87-aed858db08fa
--------
user
# 审查请求：undertow 期权定位分析工具（2026-08-31 一天的 9 个提交）

这是一个服务真实小账户（净资产 $264）的期权分析工具，纯标准库、零依赖。
用户会**照着它的输出下真金白银的单**。今天新增了一整条从「信号」到「仓位」的链路，
需要你重点找**会让人亏钱的错误**，而不是代码风格。

## 项目铁律（违反即为严重问题）
1. LLM 不做算术，所有数字来自确定性代码
2. 任何进入交易决策的判断，必须有回测记录（样本量 + p 值），未验证的必须标注
3. 回测统计单位是【日期簇】（跨品种同日高度相关：金银 0.89、QQQ/TQQQ 0.99）
4. 不得有前瞻偏差（look-ahead）：只能用信号产生时点【已知】的数据
5. 报告不得只给结论不给样本量

## 今天新增的 5 个模块（重点审查）
- `undertow/analyze/validation.py` —— 验证状态登记簿
- `undertow/analyze/cost_gate.py` —— 预期波动 vs 回本门槛
- `undertow/analyze/credit_wall.py` —— 墙位卖方价差（三风险档）
- `undertow/analyze/sizing.py` —— Kelly 仓位
- `undertow/analyze/backmonth.py` —— 远月异动扫描

修改：`analyze/gamma.py`（到期分层）、`analyze/flow.py`（可交易闸门、比例价差）、
`analyze/outlook.py`、`report/html.py`、`cli.py`

## 需要你重点质疑的具体结论

### A. 卖方价差回测（credit_wall.py 顶部注释）
口径：快照 D 盘前建仓（用快照的 bid/ask，卖腿吃 bid、买腿吃 ask），
持有到期，用到期日实际收盘价算内在价值。手续费 4 腿 × $0.80。
结论：
- 近端【加总】墙（≤14天所有到期相加）比【同到期专属】墙好得多
  （加总 ≥15%：14笔 86%胜率 +9.73%/笔；同到期 ≥25%：55笔 76% -5.26%/笔）
- 三档：稳健(墙外2%/宽2%/15~45天) 82%胜率 +2.84%；
  激进(墙内2%/宽2.5%/4~14天) 63%胜率 +9.99%
质疑点：
1. 「加总墙优于同到期墙」是否是幸存者偏差？我用「提高同到期门槛到样本量相近」
   来排除，这个排除方法站得住吗？
2. 用快照 bid/ask 当成交价，是否高估？盘前快照的盘口是否代表可成交价？
3. 持有到期用收盘价结算——美式期权可提前行权，卖方有被提前指派的风险，
   回测完全没考虑，这个遗漏有多严重？
4. 45 个信号 × 多个到期 = 60+ 笔，但这些笔【共享同一个信号】，
   是否违反独立性假设？日期簇口径在这里该怎么算？

### B. 提前平仓的结论（反直觉）
用每日快照的真实 bid/ask 逐日重估，模拟「赚到 X% 就平」。
结论：提前平仓【更差】（卖墙上：持有到期 +40% vs 赚50%平 +5%）。
质疑点：这个结论与业界通行做法相反。是我的模拟有问题（比如平仓成本算法
`买回卖腿ask - 卖掉买腿bid` 是否过于悲观），还是结论真的成立？

### C. Kelly 仓位（sizing.py）
由「胜率 p + 单笔期望 + 赢时收益率」反解输时亏损率，再算 f*=(pb−q)/b。
结论：激进档(63%胜率) Kelly 50% > 稳健档(82%胜率) Kelly 24%，
因为激进档盈亏比 2.84 vs 稳健档 0.31。
质疑点：
1. 反解 lose_roi 用的是【平均】亏损，但期权价差的亏损分布是双峰的
   （要么小亏要么全损），用平均值算 Kelly 是否严重低估风险？
2. Kelly 假设可无限分割且可重复无限次，对「1 组不可分 + 样本 38~60 笔」
   的现实，这个框架本身是否适用？
3. `size()` 在 1 组超过 Kelly 时仍返回 ok=True 并建议下 1 组，
   这是否等于鼓励超配？

### D. 可交易闸门（flow.py::tradeable_info）
压力倍数 <2× 判为「今天没信息」。放行组 65%、拦掉组 41%，Fisher p=0.044。
我自己标注了「测了 10 个阈值，Bonferroni 后 p=0.44 不显著」。
质疑点：既然 Bonferroni 后不显著，把它做成硬闸门（直接压制方向结论）是否过早？

### E. 预期波动表（cost_gate.py::EXPECTED_MOVE_TABLE）
由可判定率预测当日波动幅度，四档（<35%→0.69%，≥80%→1.84%）。
相关性 r=+0.243 (n=66, t=2.01, p≈0.048)。
质疑点：≥80% 档只有 6 笔却给出 1.84% 的点估计，报告里直接拿它跟
回本门槛比大小做「做/不做」的判定，这个用法是否超出了数据的支撑力？

### F. 前瞻偏差专项
请逐个检查这些回测是否用了信号时点不可知的信息：
- `layered_walls` / `_walls_aggregate` 用的 `today` 参数
  （报告用 obs_day = 快照日前一工作日，回测里是否一致？）
- 到期日结算价 `px_on()` 取「到期日或之后最近一天」的收盘 —— 是否合理？
- 预期波动表是用全样本算的，再用回全样本 —— 这是否是 in-sample 污染？

## 已知并已标注的问题（不用重复报，除非你认为标注不够）
- 所有阈值在同一批数据上选出，多重比较风险
- 年化是「单笔 × 365/持有天数」外推，未扣信号空窗
- 样本区间仅 2026-06-25 ~ 08-31，且期间 GLD +31%（牛市偏差）
- Gamma 墙位空间因子 49%（不及格），已登记但 weight 未降

请按严重度输出：P0（会直接导致亏钱/错误决策）、P1（统计或口径错误）、
P2（可改进）。对每条给出具体文件行号和你的判断依据。
hook: UserPromptSubmit
hook: UserPromptSubmit Completed
codex
我会按“审查、只读、不改代码”来做，先复现 9 个提交的差异与测试口径，再逐项追踪 A–F 的计算链。重点只报能改变真实交易决策的缺陷，并给精确行号。
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc 'git status --short && git log --oneline -12 && rg -n "EXPECTED_MOVE_TABLE|tradeable_info|layered_walls|_walls_aggregate|px_on|Kelly|kelly|lose_roi|early|take.profit|profit" undertow tests' in /Users/yhdong/Trading
 succeeded in 344ms:
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-zNPwanOF' (errno=Operation not permitted)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-FuCH7QGM' (errno=Operation not permitted)
0d3fdfb chore: 报告归档改为按月分目录，清理根目录 90 个同日重跑残留
c3b744e feat: 仓位改用 Kelly，废掉「净资产固定百分比」—— 它对小账户是强制亏损
ef32782 feat: 研报接入墙位卖方价差（期权结构下方）+ index 精简
bb22ef6 feat: 墙位卖方价差模块 —— 三个风险档，卖腿位置/宽度/到期全网格实测
7fd5c43 feat: 补完 R15 比例价差 + R16 远月异动扫描；曲面闸门四口径对比后维持原样
1fd06cf feat: 验证状态登记簿 —— 每条判断都要摊开实测成绩，包括不及格的
d3422e4 feat: 成本闸门 —— 预期波动 vs 回本门槛并排，否掉「方向对但幅度不够」的交易
b20505b feat: 可交易信息闸门 —— 横盘日直接说「今天没信息」，不再照样给方向
a37d52f feat: 期权结构按到期分层（近端置顶）—— 混算墙位是加总产物，实盘不存在
4ec412f 每日自动更新 2026-08-31：期权链快照+四品种报告（launchd 定时任务）
a007b2d feat: 品种对比值观察 —— 金银比逐日记录，只记不判
6158453 feat: 长桥时效验证扩到 OI —— 收盘后何时更新，是不是不用等凌晨 2 点
undertow/consult/packet.py:289:            mp = "—" if c["max_profit"] is None else f"最大盈 ${c['max_profit']:,.0f}"
undertow/consult/packet.py:318:                    mp = "—" if c["max_profit"] is None else f"最大盈 ${c['max_profit']:,.0f}"
tests/test_portfolio.py:66:    assert abs(s.max_profit - 0.19 * 100 * 4) < 1e-6, s.max_profit   # 76
tests/test_portfolio.py:68:    print(f"PASS test_bull_put_spread_detected → {s.note} 最大盈{s.max_profit:.0f}/最大亏{s.max_loss:.0f}")
tests/test_verdict.py:48:def test_uptrend_extended_take_profit():
tests/test_verdict.py:59:    print(f"PASS test_uptrend_extended_take_profit → {v.headline}")
undertow/analyze/gamma.py:70:    # 到期分层墙位（见 layered_walls 的注释）。call_wall/put_wall 主字段取【近端】，
undertow/analyze/gamma.py:202:    layers = layered_walls(snap, today, spot)
undertow/analyze/gamma.py:505:def layered_walls(snap, today: date, spot: float) -> dict[str, WallLayer]:
undertow/analyze/sizing.py:1:"""仓位：按 Kelly 算，不按净资产的固定百分比。
undertow/analyze/sizing.py:13:【正确的框架】先确认策略正期望，再用 Kelly 定仓位：
undertow/analyze/sizing.py:17:    策略        胜率   单笔     赢时    输时     盈亏比b   Kelly  1张占用/净资产
undertow/analyze/sizing.py:28:⚠️ Kelly 假设期望估计准确。样本 38~60 笔、阈值在同一批数据上选出，
undertow/analyze/sizing.py:29:   估计偏乐观。半 Kelly 是常用折中；但对期权价差，1 组是最小不可分单位，
undertow/analyze/sizing.py:30:   往往已经超过半 Kelly —— 那时的选择只有「按 1 组做」或「不做」，
undertow/analyze/sizing.py:31:   不存在"缩小到半 Kelly"这个选项。
undertow/analyze/sizing.py:42:class KellyResult:
undertow/analyze/sizing.py:45:    lose_roi: float         # 输时相对占用的亏损率（正数）
undertow/analyze/sizing.py:46:    odds: float             # 盈亏比 b = win_roi / lose_roi（平均赚 ÷ 平均亏）
undertow/analyze/sizing.py:47:    kelly: float            # f*
undertow/analyze/sizing.py:48:    half_kelly: float
undertow/analyze/sizing.py:56:def kelly(win_rate: float, per_trade_pct: float, win_roi_pct: float) -> KellyResult:
undertow/analyze/sizing.py:57:    """由「胜率 + 单笔期望 + 赢时收益率」反解输时亏损率，再算 Kelly。
undertow/analyze/sizing.py:59:    per_trade_pct = p·win_roi + (1−p)·lose_roi  （都是 %）
undertow/analyze/sizing.py:65:        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
undertow/analyze/sizing.py:66:    lose_roi = abs((per_trade_pct / 100.0 - p * win_roi) / q)
undertow/analyze/sizing.py:67:    if lose_roi <= 0:
undertow/analyze/sizing.py:68:        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
undertow/analyze/sizing.py:69:    b = win_roi / lose_roi
undertow/analyze/sizing.py:72:    return KellyResult(p, win_roi, lose_roi, b, max(0.0, f), max(0.0, f / 2), edge)
undertow/analyze/sizing.py:79:    kelly_dollars: float
undertow/analyze/sizing.py:82:    kelly_frac: float
undertow/analyze/sizing.py:83:    over_kelly: float       # 实际 / Kelly，>1 = 超配
undertow/analyze/sizing.py:87:def size(net_assets: float, unit_occupancy: float, k: KellyResult,
undertow/analyze/sizing.py:89:         max_over_kelly: float = 1.5) -> SizeVerdict:
undertow/analyze/sizing.py:90:    """给出该下几组。1 组已超 Kelly 时明说超了多少 —— 由人决定要不要下。
undertow/analyze/sizing.py:92:    不静默把仓位压到 0：对小账户，「按 Kelly 缩小」常常等于「不能交易」，
undertow/analyze/sizing.py:100:        return SizeVerdict(False, 0, 0.0, unit_occupancy, 0.0, k.kelly, 0.0,
undertow/analyze/sizing.py:102:    kd = net_assets * k.kelly
undertow/analyze/sizing.py:105:    over = frac1 / k.kelly if k.kelly > 0 else float("inf")
undertow/analyze/sizing.py:108:        return SizeVerdict(False, 0, kd, unit_occupancy, frac1, k.kelly, over,
undertow/analyze/sizing.py:112:        return SizeVerdict(True, n, kd, unit_occupancy, n * frac1, k.kelly,
undertow/analyze/sizing.py:113:                           n * frac1 / k.kelly,
undertow/analyze/sizing.py:114:                           f"Kelly ${kd:.0f} → {n} 组（每组 ${unit_occupancy:.0f}）。"
undertow/analyze/sizing.py:116:    # 1 组已超 Kelly：把超配倍数摆出来，不替人做决定
undertow/analyze/sizing.py:117:    lvl = ("可接受" if over <= max_over_kelly else "明显超配")
undertow/analyze/sizing.py:118:    return SizeVerdict(True, 1, kd, unit_occupancy, frac1, k.kelly, over,
undertow/analyze/sizing.py:119:                       f"Kelly 只允许 ${kd:.0f}，但 1 组要 ${unit_occupancy:.0f}"
undertow/analyze/sizing.py:120:                       f"（净资产 {frac1:.0%}，是 Kelly 的 {over:.1f} 倍·{lvl}）。"
undertow/analyze/sizing.py:122:                       f"把仓位压到 Kelly 以下的选项不存在。")
tests/test_position_alert.py:575:    assert "def layered_walls" in gsrc and "def wall_agreement" in gsrc
tests/test_position_alert.py:681:    from undertow.analyze.flow import (tradeable_info, TRADEABLE_MIN_RATIO,
tests/test_position_alert.py:711:    t = tradeable_info(_F([_C(1100, "bearish"), _C(1000, "bullish")]))
tests/test_position_alert.py:715:    t2 = tradeable_info(_F([_C(1000, "bearish"), _C(5700, "bullish")]))
tests/test_position_alert.py:718:    t3 = tradeable_info(_F([_C(5000, "bearish"), _C(100, "bullish"),
tests/test_position_alert.py:931:def test_sizing_is_kelly_not_fixed_pct():
tests/test_position_alert.py:932:    """仓位按 Kelly 判，不按净资产固定百分比。
tests/test_position_alert.py:943:      ② 1 组超过 Kelly 时不得静默压到 0 —— 那等于"不能交易"，
tests/test_position_alert.py:947:    from undertow.analyze.sizing import kelly, size, ruin_probability
tests/test_position_alert.py:953:    kc = kelly(cons["win_rate"], cons["per_trade_pct"], cons["win_roi_pct"])
tests/test_position_alert.py:954:    ka = kelly(aggr["win_rate"], aggr["per_trade_pct"], aggr["win_roi_pct"])
tests/test_position_alert.py:957:    assert ka.kelly > kc.kelly, "Kelly 应据此给激进档更大仓位"
tests/test_position_alert.py:958:    assert kc.lose_roi > ka.lose_roi, "稳健档输的时候亏得更多（卖得远=被打穿即深实值）"
tests/test_position_alert.py:961:    bad = kelly(0.30, -5.0, 10.0)
tests/test_position_alert.py:966:    # ② 1 组超 Kelly 不得静默归零
tests/test_position_alert.py:968:    assert v2.ok and v2.n_units == 1, "不得因超 Kelly 就判成不能做"
tests/test_position_alert.py:969:    assert v2.over_kelly > 1.0
tests/test_position_alert.py:970:    assert "不存在" in v2.reason, "必须说明「压到 Kelly 以下的选项不存在」"
tests/test_position_alert.py:984:    print("PASS test_sizing_is_kelly_not_fixed_pct")
tests/test_healthcheck.py:200:    assert abs(gross - (0.80 * c.max_profit - 0.20 * c.max_loss)) < 1e-6
undertow/analyze/portfolio.py:316:    max_profit: float | None
undertow/analyze/portfolio.py:435:                 net_credit=v.credit, max_profit=mp, max_loss=ml,
undertow/analyze/portfolio.py:443:    max_profit = total_credit * CONTRACT_MULT * qty
undertow/analyze/portfolio.py:451:                 net_credit=total_credit, max_profit=max_profit, max_loss=max_loss,
undertow/analyze/portfolio.py:487:                             net_credit=credit, max_profit=None, max_loss=mloss,
undertow/analyze/portfolio.py:517:                 max_profit=(prem if short else None), max_loss=mloss,
undertow/analyze/portfolio.py:548:                                 net_credit=None, max_profit=None, max_loss=None,
undertow/soul/profile.py:232:            elif (not is_credit) and lim.min_rr is not None and c.max_profit and c.max_loss:
undertow/soul/profile.py:233:                rr = c.max_profit / c.max_loss
undertow/report/html.py:1088:    if cp.max_profit is not None:
undertow/report/html.py:1089:        pnl.append(f"理论净收 <b>${cp.max_profit:,.0f}</b>")
undertow/report/html.py:1155:    if cp.max_profit is not None:
undertow/report/html.py:1156:        pnl.append(f"理论净收 <b>${cp.max_profit:,.0f}</b>")
undertow/report/html.py:2045:                    mp = "—" if c.max_profit is None else f'<b style="color:#1a7f37">{c.max_profit:+,.0f}</b>'
undertow/report/html.py:2438:        # 仓位按 Kelly 判，不按净资产固定百分比 —— 用户 2026-08-31：
undertow/report/html.py:2443:                from undertow.analyze.sizing import kelly as _kel, size as _sz, ruin_probability
undertow/report/html.py:2450:                _c = "#1a7f37" if _v2.over_kelly <= 1.5 else "#bc4c00"
undertow/report/html.py:2453:                       f'<b>仓位</b>　盈亏比 {_k.odds:.2f}　Kelly {_k.kelly:.0%}'
undertow/report/html.py:2454:                       f'（${net_assets * _k.kelly:.0f}）　'
undertow/report/html.py:2456:                       f'{_occ / net_assets:.0%}，Kelly 的 {_v2.over_kelly:.1f} 倍</span>'
undertow/report/html.py:2525:        '⚠️ <b>仓位按 Kelly 判，不按净资产固定百分比。</b>'
undertow/report/html.py:2528:        '期权价差 1 组是最小不可分单位，超过 Kelly 时只有「按 1 组做」或「不做」，'
undertow/report/markdown.py:580:                mp = "—" if c.max_profit is None else f"{c.max_profit:+,.0f}"
undertow/analyze/credit_spread.py:70:    max_profit: float | None = None
undertow/analyze/credit_spread.py:201:    max_profit = net_credit * 100.0
undertow/analyze/credit_spread.py:271:                f"理论净收 ${max_profit:.0f}、最大亏损 ${max_loss:.0f}"
undertow/analyze/credit_spread.py:278:        net_credit=net_credit, width=width, max_profit=max_profit, max_loss=max_loss,
undertow/analyze/credit_wall.py:148:def _walls_aggregate(snap, obs: date, kind: str, spot: float, max_dte: int = 14):
undertow/analyze/credit_wall.py:216:    wk, woi, wshare = _walls_aggregate(snap, obs, kind, spot)
undertow/cli.py:42:from undertow.analyze.flow import (analyze_flow, counter_signals, tradeable_info, detect_ratio_spreads,
undertow/cli.py:353:            from undertow.analyze.flow import tradeable_info as _ti_fn
undertow/cli.py:1405:            # —— 可交易信息闸门（压力倍数 <2× = 今天没信息，见 flow.tradeable_info）——
undertow/cli.py:1408:                _ti = tradeable_info(fa)
undertow/analyze/condor.py:70:    max_profit: float | None = None          # $ / 组合
undertow/analyze/condor.py:219:    max_profit = net_credit * 100.0
undertow/analyze/condor.py:334:                f"买 {fmt(buy_put.strike)}P/{fmt(buy_call.strike)}C，理论净收 ${max_profit:.0f}"
undertow/analyze/condor.py:343:        max_profit=max_profit, max_loss=max_loss, rr=rr, be_lo=be_lo, be_hi=be_hi,
undertow/analyze/healthcheck.py:21:POOR_RR_SELLER = 1.0         # 方向性/借方结构 max_profit/max_loss < 此 = 盈亏比偏低
undertow/analyze/healthcheck.py:45:def _breakeven_winrate(max_profit, max_loss) -> float | None:
undertow/analyze/healthcheck.py:47:    if not max_profit or not max_loss:
undertow/analyze/healthcheck.py:49:    mp, ml = abs(max_profit), abs(max_loss)
undertow/analyze/healthcheck.py:65:    be = _breakeven_winrate(combo.max_profit, combo.max_loss)
undertow/analyze/healthcheck.py:96:    if not combo.max_profit or not combo.max_loss or not spot or spot <= 0:
undertow/analyze/healthcheck.py:99:    be_wr = combo.max_loss / (combo.max_loss + combo.max_profit)
undertow/analyze/healthcheck.py:163:    if not combo.max_profit or not combo.max_loss:
undertow/analyze/healthcheck.py:172:    gross = implied_p * combo.max_profit - (1 - implied_p) * combo.max_loss
undertow/analyze/healthcheck.py:267:        is_credit = bool(c.net_credit and c.net_credit > 0 and c.max_profit and c.max_loss)
undertow/analyze/healthcheck.py:284:            wr = _breakeven_winrate(c.max_profit, c.max_loss)
undertow/analyze/healthcheck.py:289:                    detail=(f"{c.label}：最大盈 ${c.max_profit:,.0f} / 最大亏 ${c.max_loss:,.0f}，"
undertow/analyze/healthcheck.py:349:            elif c.max_profit and c.max_loss and (c.max_profit / c.max_loss) < POOR_RR_SELLER:
undertow/analyze/healthcheck.py:352:                    detail=f"{c.label}：最大盈 ${c.max_profit:,.0f} / 最大亏 ${c.max_loss:,.0f}"
undertow/analyze/healthcheck.py:353:                           f"（R:R {c.max_profit / c.max_loss:.2f} < 1）",
undertow/analyze/cost_gate.py:42:EXPECTED_MOVE_TABLE = [
undertow/analyze/cost_gate.py:68:    for lo, hi, pct, n, p1 in EXPECTED_MOVE_TABLE:
undertow/analyze/cost_gate.py:72:    return ExpectedMove(pct=EXPECTED_MOVE_TABLE[-1][2], n=EXPECTED_MOVE_TABLE[-1][3],
undertow/analyze/cost_gate.py:73:                        p_over_1pct=EXPECTED_MOVE_TABLE[-1][4], band="≥80%")
undertow/analyze/flow.py:2124:def tradeable_info(fa) -> dict:

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc 'git diff a007b2d..c3b744e --stat && nl -ba undertow/analyze/validation.py && nl -ba undertow/analyze/cost_gate.py && nl -ba undertow/analyze/sizing.py && nl -ba undertow/analyze/credit_wall.py' in /Users/yhdong/Trading
 succeeded in 37ms:
 data/history/outlook_scores.json               |  32 +-
 data/history/ratio_watch.json                  |  44 ++
 data/history/resonance/gold.json               |  34 ++
 data/history/resonance/iwm.json                |  34 ++
 data/history/resonance/qqq.json                |  34 ++
 data/history/resonance/silver.json             |  34 ++
 data/history/resonance/spy.json                |  34 ++
 data/history/resonance/tlt.json                |  34 ++
 data/history/resonance/tqqq.json               |  34 ++
 data/history/resonance/wti.json                |  34 ++
 data/history/signals/gold.json                 |  87 ++++
 data/history/signals/iwm.json                  |  91 +++-
 data/history/signals/qqq.json                  |  85 ++++
 data/history/signals/silver.json               |  90 +++-
 data/history/signals/spy.json                  |  85 ++++
 data/history/signals/tlt.json                  |  91 +++-
 data/history/signals/tqqq.json                 |  87 ++++
 data/history/signals/wti.json                  |  90 +++-
 data/history/spreads/2026-08-30.jsonl          |  18 +
 data/history/spreads/2026-08-31.jsonl          |  17 +
 data/history/vrp/gold.json                     |  24 +-
 data/history/vrp/iwm.json                      |  24 +-
 data/history/vrp/qqq.json                      |  24 +-
 data/history/vrp/silver.json                   |  24 +-
 data/history/vrp/spy.json                      |  24 +-
 data/history/vrp/tlt.json                      |  24 +-
 data/history/vrp/tqqq.json                     |  20 +-
 data/history/vrp/wti.json                      |  24 +-
 data/logs/daily_2026-08.log                    |  90 ++++
 data/logs/lb_timing_2026-08-31.log             |  77 +++
 data/logs/lb_timing_2026-09-01.log             |   5 +
 data/logs/session_2026-08.log                  |   9 +
 data/logs/settlement_2026-08-30.log            |  68 +++
 data/reports/ALERT_2026-08-31.txt              |   3 +
 data/reports/gold_2026-08-28.html              |  26 +
 data/reports/gold_2026-08-31.html              |  26 +
 data/reports/gold_2026-08-31_r1018.html        |  26 +
 data/reports/gold_2026-08-31_r1031.html        |  26 +
 data/reports/gold_2026-08-31_r1034.html        |  26 +
 data/reports/gold_2026-08-31_r1043.html        |  26 +
 data/reports/gold_2026-08-31_r1255.html        |  26 +
 data/reports/gold_2026-08-31_r1258.html        |  26 +
 data/reports/gold_2026-08-31_r1300.html        |  26 +
 data/reports/gold_2026-08-31_r1302.html        |  26 +
 data/reports/gold_2026-08-31_r1800.html        |  26 +
 data/reports/gold_2026-08-31_r1927.html        |  26 +
 data/reports/gold_2026-08-31_r2127.html        |  26 +
 data/reports/gold_2026-08-31_r2129.html        |  26 +
 data/reports/gold_2026-08-31_r2130.html        |  26 +
 data/reports/gold_2026-08-31_r2133.html        |  26 +
 data/reports/index_2026-08-31.html             |  26 +
 data/reports/index_2026-08-31_r1019.html       |  26 +
 data/reports/index_2026-08-31_r1031.html       |  26 +
 data/reports/index_2026-08-31_r1044.html       |  26 +
 data/reports/index_2026-08-31_r1256.html       |  26 +
 data/reports/index_2026-08-31_r1258.html       |  26 +
 data/reports/index_2026-08-31_r1300.html       |  26 +
 data/reports/index_2026-08-31_r1302.html       |  26 +
 data/reports/index_2026-08-31_r1711.html       |  26 +
 data/reports/index_2026-08-31_r1801.html       |  26 +
 data/reports/index_2026-08-31_r1929.html       |  26 +
 data/reports/index_2026-08-31_r2129.html       |  26 +
 data/reports/index_2026-08-31_r2130.html       |  26 +
 data/reports/iwm_2026-08-28.html               |  26 +
 data/reports/iwm_2026-08-31.html               |  26 +
 data/reports/iwm_2026-08-31_r1019.html         |  26 +
 data/reports/iwm_2026-08-31_r1044.html         |  26 +
 data/reports/iwm_2026-08-31_r1256.html         |  26 +
 data/reports/iwm_2026-08-31_r1300.html         |  26 +
 data/reports/iwm_2026-08-31_r1302.html         |  26 +
 data/reports/iwm_2026-08-31_r1801.html         |  26 +
 data/reports/iwm_2026-08-31_r1929.html         |  26 +
 data/reports/iwm_2026-08-31_r2130.html         |  26 +
 data/reports/qqq_2026-08-31.html               |  26 +
 data/reports/qqq_2026-08-31_r1019.html         |  26 +
 data/reports/qqq_2026-08-31_r1044.html         |  26 +
 data/reports/qqq_2026-08-31_r1256.html         |  26 +
 data/reports/qqq_2026-08-31_r1300.html         |  26 +
 data/reports/qqq_2026-08-31_r1302.html         |  26 +
 data/reports/qqq_2026-08-31_r1710.html         |  26 +
 data/reports/qqq_2026-08-31_r1801.html         |  26 +
 data/reports/qqq_2026-08-31_r1928.html         |  26 +
 data/reports/qqq_2026-08-31_r2130.html         |  26 +
 data/reports/silver_2026-08-28.html            |  26 +
 data/reports/silver_2026-08-31.html            |  26 +
 data/reports/silver_2026-08-31_r1018.html      |  26 +
 data/reports/silver_2026-08-31_r1031.html      |  26 +
 data/reports/silver_2026-08-31_r1041.html      |  26 +
 data/reports/silver_2026-08-31_r1043.html      |  26 +
 data/reports/silver_2026-08-31_r1255.html      |  26 +
 data/reports/silver_2026-08-31_r1258.html      |  26 +
 data/reports/silver_2026-08-31_r1300.html      |  26 +
 data/reports/silver_2026-08-31_r1302.html      |  26 +
 data/reports/silver_2026-08-31_r1303.html      |  26 +
 data/reports/silver_2026-08-31_r1310.html      |  26 +
 data/reports/silver_2026-08-31_r1800.html      |  26 +
 data/reports/silver_2026-08-31_r1927.html      |  26 +
 data/reports/silver_2026-08-31_r2129.html      |  26 +
 data/reports/silver_2026-08-31_r2130.html      |  26 +
 data/reports/spy_2026-08-31.html               |  26 +
 data/reports/spy_2026-08-31_r1019.html         |  26 +
 data/reports/spy_2026-08-31_r1044.html         |  26 +
 data/reports/spy_2026-08-31_r1256.html         |  26 +
 data/reports/spy_2026-08-31_r1300.html         |  26 +
 data/reports/spy_2026-08-31_r1302.html         |  26 +
 data/reports/spy_2026-08-31_r1711.html         |  26 +
 data/reports/spy_2026-08-31_r1801.html         |  26 +
 data/reports/spy_2026-08-31_r1928.html         |  26 +
 data/reports/spy_2026-08-31_r2130.html         |  26 +
 data/reports/tlt_2026-08-28.html               |  26 +
 data/reports/tlt_2026-08-31.html               |  26 +
 data/reports/tlt_2026-08-31_r1019.html         |  26 +
 data/reports/tlt_2026-08-31_r1044.html         |  26 +
 data/reports/tlt_2026-08-31_r1256.html         |  26 +
 data/reports/tlt_2026-08-31_r1300.html         |  26 +
 data/reports/tlt_2026-08-31_r1302.html         |  26 +
 data/reports/tlt_2026-08-31_r1801.html         |  26 +
 data/reports/tlt_2026-08-31_r1928.html         |  26 +
 data/reports/tlt_2026-08-31_r2130.html         |  26 +
 data/reports/tqqq_2026-08-28.html              |  26 +
 data/reports/tqqq_2026-08-31.html              |  26 +
 data/reports/tqqq_2026-08-31_r1019.html        |  26 +
 data/reports/tqqq_2026-08-31_r1044.html        |  26 +
 data/reports/tqqq_2026-08-31_r1256.html        |  26 +
 data/reports/tqqq_2026-08-31_r1300.html        |  26 +
 data/reports/tqqq_2026-08-31_r1302.html        |  26 +
 data/reports/tqqq_2026-08-31_r1801.html        |  26 +
 data/reports/tqqq_2026-08-31_r1928.html        |  26 +
 data/reports/tqqq_2026-08-31_r2130.html        |  26 +
 data/reports/wti_2026-08-28.html               |  26 +
 data/reports/wti_2026-08-31.html               |  26 +
 data/reports/wti_2026-08-31_r1018.html         |  26 +
 data/reports/wti_2026-08-31_r1044.html         |  26 +
 data/reports/wti_2026-08-31_r1255.html         |  26 +
 data/reports/wti_2026-08-31_r1300.html         |  26 +
 data/reports/wti_2026-08-31_r1302.html         |  26 +
 data/reports/wti_2026-08-31_r1800.html         |  26 +
 data/reports/wti_2026-08-31_r1928.html         |  26 +
 data/reports/wti_2026-08-31_r2130.html         |  26 +
 data/snapshots/options/GLD/2026-08-31.json.gz  | Bin 0 -> 466559 bytes
 data/snapshots/options/IWM/2026-08-31.json.gz  | Bin 0 -> 310913 bytes
 data/snapshots/options/QQQ/2026-08-31.json.gz  | Bin 0 -> 679795 bytes
 data/snapshots/options/SLV/2026-08-31.json.gz  | Bin 0 -> 297858 bytes
 data/snapshots/options/SPY/2026-08-31.json.gz  | Bin 0 -> 778057 bytes
 data/snapshots/options/TLT/2026-08-31.json.gz  | Bin 0 -> 139968 bytes
 data/snapshots/options/TQQQ/2026-08-31.json.gz | Bin 0 -> 106113 bytes
 data/snapshots/options/USO/2026-08-31.json.gz  | Bin 0 -> 249908 bytes
 scripts/verify_intraday_lead.sh                |   6 +-
 tests/test_position_alert.py                   | 353 +++++++++++++-
 undertow/analyze/backmonth.py                  | 113 +++++
 undertow/analyze/cost_gate.py                  | 193 ++++++++
 undertow/analyze/credit_wall.py                | 300 ++++++++++++
 undertow/analyze/flow.py                       | 196 ++++++++
 undertow/analyze/gamma.py                      | 242 ++++++++-
 undertow/analyze/outlook.py                    |  27 +-
 undertow/analyze/sizing.py                     | 127 +++++
 undertow/analyze/validation.py                 | 181 +++++++
 undertow/cli.py                                | 136 +++++-
 undertow/report/html.py                        | 649 +++++++++++++++++++++++--
 undertow/report/markdown.py                    |  28 ++
 160 files changed, 6642 insertions(+), 168 deletions(-)
     1	"""验证状态登记簿：每个会影响交易决策的判断，必须在这里登记它的实测成绩。
     2	
     3	起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，你又说
     4	这其实是不准的」。根因是报告用「回测校准的加权投票」这种说法呈现结论，而
     5	翻遍代码，八个投票因子里只有一个有完整的回测记录（样本量 + p 值），其余的
     6	权重是拍的 —— outlook.py 开头那句「按回测校准的可信度加权」当时并不成立。
     7	
     8	规则（写死在测试里）：
     9	  1. 任何进入交易决策的判断，都要在 REGISTRY 里有一条，带 n / hits / p_value；
    10	  2. 报告展示结论时，必须同时展示 n 与 p，不得只展示结论；
    11	  3. 未验证的判断标 status="未验证"，不得给「可信度中/高」这类标签；
    12	  4. p ≥ 0.05 时必须显示 samples_to_significance()，把「什么时候能信」
    13	     变成一个具体数字，而不是含糊的「样本不足」。
    14	
    15	⚠️ 全部数字来自 2026-08-31 的回测，样本区间 2026-06-25 ~ 2026-08-31。
    16	   新增样本后需重跑并更新此表，不得手改。
    17	"""
    18	from __future__ import annotations
    19	
    20	import math
    21	from dataclasses import dataclass
    22	
    23	
    24	def binom_p(k: int, n: int, p0: float = 0.5) -> float:
    25	    """双侧二项检验：n 次里 k 次命中，与 p0 的差异有多容易被运气解释。"""
    26	    if n <= 0:
    27	        return 1.0
    28	    return min(1.0, 2 * sum(math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i)
    29	                            for i in range(k, n + 1)))
    30	
    31	
    32	def samples_to_significance(hits: int, n: int, p0: float = 0.5,
    33	                            alpha: float = 0.05, cap: int = 5000) -> int | None:
    34	    """保持当前命中率，还需要多少个样本才能达到显著。
    35	
    36	    回答的是「什么时候能信这个信号」—— 用户 2026-08-31 问的正是这个。
    37	    返回 None = 当前命中率太接近 p0，在 cap 之内达不到（这本身就是答案：
    38	    这个信号即使是真的，边缘也太薄，不值得等）。
    39	    """
    40	    if n <= 0:
    41	        return None
    42	    rate = hits / n
    43	    if rate <= p0:
    44	        return None
    45	    m = n
    46	    while m < cap:
    47	        m += 1
    48	        k = math.ceil(rate * m - 1e-9)
    49	        if binom_p(k, m, p0) < alpha:
    50	            return m - n
    51	    return None
    52	
    53	
    54	@dataclass(frozen=True)
    55	class Validation:
    56	    key: str
    57	    label: str
    58	    n: int
    59	    hits: int
    60	    p_value: float
    61	    baseline: float          # 对照基准（无脑做多的胜率等）
    62	    note: str
    63	    caveat: str = ""
    64	    cluster_n: int | None = None   # 日期簇数（跨品种同日相关，簇才是独立样本）
    65	    # kind="hit" 命中率检验（hits/n + 二项检验）
    66	    # kind="corr" 相关性检验（r/t + t 检验）—— 此时 hits 无意义，不得当命中率显示
    67	    kind: str = "hit"
    68	    r: float | None = None         # 相关型：相关系数
    69	    r_control: float | None = None # 相关型：对照相关系数（如"对命中率"的 r）
    70	    effect: str = ""               # 相关型：效应的人话描述
    71	
    72	    @property
    73	    def rate(self) -> float:
    74	        return self.hits / self.n if self.n else 0.0
    75	
    76	    @property
    77	    def significant(self) -> bool:
    78	        return self.p_value < 0.05
    79	
    80	    @property
    81	    def status(self) -> str:
    82	        if self.n == 0:
    83	            return "未验证"
    84	        if self.significant:
    85	            return "已验证"
    86	        return "样本不足"
    87	
    88	    @property
    89	    def need_more(self) -> int | None:
    90	        return samples_to_significance(self.hits, self.n)
    91	
    92	    def summary(self) -> str:
    93	        if self.kind == "corr":
    94	            # 相关型：hits 无意义，显示 r 与对照 r
    95	            s = f"n={self.n}，r={self.r:+.3f}，p={self.p_value:.3f}"
    96	            if self.r_control is not None:
    97	                s += f"（对照 r={self.r_control:+.3f}）"
    98	            if self.effect:
    99	                s += f" · {self.effect}"
   100	            return s + (" · 已验证" if self.significant else " · 未达显著")
   101	        s = f"{self.hits}/{self.n} = {self.rate:.0%}（基准 {self.baseline:.0%}），p={self.p_value:.3f}"
   102	        if self.cluster_n:
   103	            s += f"，{self.cluster_n} 个日期簇"
   104	        if self.significant:
   105	            return s + " · 已验证"
   106	        more = self.need_more
   107	        # ⚠️ need_more 假设命中率原地不变，是【乐观】下界；把抽样波动算进去
   108	        # （功效 80%）需要的样本更多 —— 强信号 65% 那条：乐观 +11，功效口径 +21。
   109	        s += (f" · 样本不足，若命中率保持不变还需 {more} 个（乐观下界；"
   110	              f"要在 80% 功效下确证需更多）"
   111	              if more else " · 命中率贴近基准，再攒样本也难以证实")
   112	        return s
   113	
   114	
   115	# ── 登记簿：每条都必须能追到一次实际回测 ────────────────────────────────────
   116	REGISTRY: dict[str, Validation] = {
   117	    "strong_signal_dir": Validation(
   118	        key="strong_signal_dir", label="强信号当日方向",
   119	        n=26, hits=17, p_value=0.169, baseline=0.50,
   120	        cluster_n=None,
   121	        note="开火信号配当日 open→close。分波动看：横盘<0.5% 时 6/12=50%（掷硬币）、"
   122	             "小动 7/10=70%、中动 3/3=100%、大动 1/1=100%。整体不显著是被横盘日稀释的。",
   123	        caveat="样本区间仅 2026-06-25 起，且横盘日占比近半"),
   124	    "tradeable_gate": Validation(
   125	        key="tradeable_gate", label="可交易信息闸门（压力比 ≥2×）",
   126	        n=62, hits=40, p_value=0.044, baseline=0.50, cluster_n=30,
   127	        note="放行组 62 笔 65%、顺向 +0.46%；拦掉组 29 笔 41%、顺向 -0.12%。Fisher p=0.044。",
   128	        caveat="共测 10 个阈值，Bonferroni 校正后 p=0.44 不再显著，待样本外验证"),
   129	    "wall_space_vote": Validation(
   130	        key="wall_space_vote", label="Gamma 墙位空间投票",
   131	        n=83, hits=41, p_value=1.000, baseline=0.51, cluster_n=43,
   132	        note="近端口径 49%、混算口径 54%、基准（无脑做多）51%。四个变体 p 全为 1.000。",
   133	        caveat="换到期口径、换阈值形式都试过，均无改善——该因子本身无预测力"),
   134	    "surface_gate": Validation(
   135	        key="surface_gate", label="固定Delta曲面闸门（逐腿与曲面矛盾即否决）",
   136	        n=66, hits=43, p_value=0.169, baseline=0.50,
   137	        note="2026-08-31 四口径对比（100 品种-日，≥2× 子集）："
   138	             "① 现状一刀切否决 65%/+0.44%　② 去掉闸门 60%/+0.31%　"
   139	             "③ 矛盾降半权 60%/+0.31%　④ 大单≥2000张豁免 64%/+0.37%。"
   140	             "放松闸门能把可判定率从 52% 提到 57%，但命中率与收益都变差 —— "
   141	             "被掐掉的不是被误伤的信号，是噪音。",
   142	        caveat="低可判定率是这套方法在诚实说「不知道」，不是待修的缺陷；"
   143	               "三种放松方案均已试过并更差，勿再改"),
   144	    "ratio_spread": Validation(
   145	        key="ratio_spread", label="比例价差检出（R15）",
   146	        kind="corr", n=4, hits=0, p_value=0.99, baseline=0.0,
   147	        r=0.0, r_control=None,
   148	        effect="信噪比（真实检出/随机基线）：纯ΔOI口径 1.0~1.5x（TQQQ 1.0x=全是巧合），"
   149	               "加当日成交量同步配对后 2.9~6.2x",
   150	        note="零假设检验：把 ΔOI 与 volume 在行权价之间随机打乱 20 次重跑。"
   151	             "GLD 2/0.7、SLV 4/1.1、QQQ 13/4.5、TQQQ 5/0.8。",
   152	        caveat="只验证了「不是随机凑对」，【未】验证检出后是否有交易价值；"
   153	               "QQQ 随机基线仍有 4.5 个，条目多时不可信"),
   154	    "expected_move": Validation(
   155	        key="expected_move", label="可判定率预告波动幅度",
   156	        kind="corr", n=66, hits=0, p_value=0.048, baseline=0.0,
   157	        r=0.243, r_control=0.053,
   158	        effect="预告的是幅度不是方向：高低两组命中率 66% vs 65%（一样），"
   159	               "当日波动 1.09% vs 0.55%（翻倍）",
   160	        note="相关性检验，非命中率检验：可判定率 vs 当日波动 r=+0.243 (t=2.01)；"
   161	             "vs 是否命中 r=+0.053（对照）。",
   162	        caveat="同批数据第二次找模式；≥80% 档仅 6 笔"),
   163	}
   164	
   165	
   166	def get(key: str) -> Validation | None:
   167	    return REGISTRY.get(key)
   168	
   169	
   170	def badge(key: str) -> str:
   171	    """给报告用的一行状态串。找不到就明说未登记，不许沉默。"""
   172	    v = REGISTRY.get(key)
   173	    if v is None:
   174	        return "⚠️ 未登记验证状态（不得作为交易依据）"
   175	    icon = {"已验证": "✅", "样本不足": "🟡", "未验证": "⚠️"}[v.status]
   176	    if v.kind == "corr":
   177	        icon = "✅" if v.significant else "🟡"
   178	    s = f"{icon} {v.status}：{v.summary()}"
   179	    if v.caveat:
   180	        s += f"　⚠️ {v.caveat}"
   181	    return s
     1	"""成本闸门：把「这天大概能走多少」和「这笔要走多少才不亏」并排放。
     2	
     3	起因（用户 2026-08-31 那晚的四笔亏损）：
     4	  SLV 9/2 到期 60P，买 0.75 卖 0.73。方向是【对】的（SLV 60 破到 59.66），
     5	  但那张 put 需要标的跌 1.9% 才覆盖点差+theta，当天只跌 0.75% → 白做。
     6	  同晚 GLD 410C 持有 1 分钟割在 0.70，两小时后 0.89 —— 那 11% 就是点差本身。
     7	
     8	两个数缺一不可：
     9	  预期波动  = 这类日子标的大概走多少（由可判定率预测，见下表）
    10	  回本门槛  = 你选的这张合约要走多少才不亏（真实 bid/ask + BS 的 Δ/θ）
    11	预期波动 < 回本门槛 → 不用等开盘就知道做不成。
    12	
    13	⚠️ 全部为【事前】量：可判定率来自盘前快照，bid/ask 是下单时的实时盘口。
    14	"""
    15	from __future__ import annotations
    16	
    17	from dataclasses import dataclass
    18	from datetime import date
    19	
    20	from undertow.analyze import blackscholes as bs
    21	
    22	# ─────────────────────────────────────────────────────────────────────────────
    23	# 预期波动：由可判定率预测（2026-08-31 实测，倍数 ≥2× 的 66 笔）
    24	#
    25	#   可判定率      笔数   命中率   当日|波动|   顺向收益   波动>1%比例
    26	#   <35%          12    67%     0.69%      +0.32%      8%
    27	#   35~60%        25    64%     0.48%      +0.24%     12%
    28	#   60~80%        23    61%     0.89%      +0.45%     39%
    29	#   ≥80%           6    83%     1.84%      +1.50%     50%
    30	#
    31	#   可判定率 vs 当日波动幅度  r=+0.243 (n=66, t=2.01, p≈0.048)
    32	#   可判定率 vs 是否命中     r=+0.053  ← 几乎为零
    33	#
    34	# 关键：可判定率预告的不是【方向准不准】（命中率 65% vs 66%，一样），
    35	#       而是【今天有没有幅度】（波动 0.55% vs 1.09%，翻倍）。
    36	#
    37	# ⚠️ 这是在同一批数据上的第二次找模式（先测倍数闸门、再测可判定率），
    38	#    多重比较风险仍在；≥80% 档只有 6 笔。分档均值只能当量级参考，
    39	#    报告必须连同样本量一起显示，不得呈现为点估计。
    40	# ─────────────────────────────────────────────────────────────────────────────
    41	
    42	EXPECTED_MOVE_TABLE = [
    43	    (0.00, 0.35, 0.69, 12, 0.08),
    44	    (0.35, 0.60, 0.48, 25, 0.12),
    45	    (0.60, 0.80, 0.89, 23, 0.39),
    46	    (0.80, 1.01, 1.84,  6, 0.50),
    47	]
    48	EXPECTED_MOVE_EVIDENCE = {
    49	    "n": 66, "r_move": 0.243, "t": 2.01, "p": 0.048, "r_hit": 0.053,
    50	    "note": "同批数据第二次找模式，需样本外验证",
    51	}
    52	
    53	
    54	@dataclass(frozen=True)
    55	class ExpectedMove:
    56	    pct: float          # 该档实测平均 |当日波动|(%)
    57	    n: int              # 该档样本量
    58	    p_over_1pct: float  # 该档里波动 >1% 的比例
    59	    band: str
    60	
    61	    @property
    62	    def weak(self) -> bool:
    63	        return self.n < 10
    64	
    65	
    66	def expected_move(decidable: float) -> ExpectedMove:
    67	    """由可判定率给出这类日子的预期波动幅度（量级参考，非点预测）。"""
    68	    for lo, hi, pct, n, p1 in EXPECTED_MOVE_TABLE:
    69	        if lo <= decidable < hi:
    70	            lab = f"{lo:.0%}~{hi:.0%}" if hi <= 1.0 else f"≥{lo:.0%}"
    71	            return ExpectedMove(pct=pct, n=n, p_over_1pct=p1, band=lab)
    72	    return ExpectedMove(pct=EXPECTED_MOVE_TABLE[-1][2], n=EXPECTED_MOVE_TABLE[-1][3],
    73	                        p_over_1pct=EXPECTED_MOVE_TABLE[-1][4], band="≥80%")
    74	
    75	
    76	@dataclass(frozen=True)
    77	class Breakeven:
    78	    symbol: str
    79	    strike: float
    80	    expiry: date
    81	    kind: str
    82	    dte: int
    83	    bid: float
    84	    ask: float
    85	    iv: float
    86	    delta: float
    87	    theta: float          # 每日（bs.theta 已是每日，不要再除 365）
    88	    spread_pct: float
    89	    cost: float           # 一张成本（含权利金，不含手续费）
    90	
    91	    def need_pct(self, spot: float, held_days: float, fee: float = 0.0) -> float:
    92	        """标的要往顺向走多少 %，这笔才刚好不亏。
    93	
    94	        need = (点差 + |θ|×持有天数 + 手续费/100) / |Δ| / 现价
    95	        """
    96	        if not self.delta or spot <= 0:
    97	            return float("inf")
    98	        spread = self.ask - self.bid
    99	        need = (spread + abs(self.theta) * held_days + fee / 100.0) / abs(self.delta)
   100	        return need / spot * 100.0
   101	
   102	    @property
   103	    def theta_share(self) -> float:
   104	        """每日损耗占权利金的比例 —— 决定这张能拿多久。"""
   105	        mid = (self.ask + self.bid) / 2
   106	        return abs(self.theta) / mid if mid > 0 else float("inf")
   107	
   108	
   109	def breakeven(symbol: str, spot: float, strike: float, expiry: date, kind: str,
   110	              today: date, bid: float, ask: float, iv: float) -> Breakeven | None:
   111	    """用真实盘口 + BS 的 Δ/θ 算这张合约的回本门槛。"""
   112	    if not (bid and ask and ask > 0 and iv and iv > 0):
   113	        return None
   114	    dte = (expiry - today).days
   115	    T = max(dte, 0.5) / 365.0
   116	    k = "C" if kind.upper().startswith("C") else "P"
   117	    dl = bs.delta(spot, strike, T, iv, k)
   118	    th = bs.theta(spot, strike, T, iv, k)   # 已是每日
   119	    mid = (ask + bid) / 2
   120	    return Breakeven(symbol=symbol, strike=strike, expiry=expiry, kind=k, dte=dte,
   121	                     bid=bid, ask=ask, iv=iv, delta=dl, theta=th,
   122	                     spread_pct=(ask - bid) / mid if mid > 0 else float("inf"),
   123	                     cost=ask * 100)
   124	
   125	
   126	@dataclass(frozen=True)
   127	class CostVerdict:
   128	    ok: bool
   129	    exp_move: ExpectedMove
   130	    need_pct: float
   131	    margin: float       # 预期波动 − 回本门槛，正=有余量
   132	    text: str
   133	
   134	
   135	def judge(be: Breakeven, spot: float, decidable: float, *,
   136	          held_days: float = 1.0, fee: float = 3.20) -> CostVerdict:
   137	    """并排对比：这天大概走多少 vs 这张要走多少才不亏。"""
   138	    em = expected_move(decidable)
   139	    need = be.need_pct(spot, held_days, fee)
   140	    margin = em.pct - need
   141	    ok = margin > 0
   142	    if ok:
   143	        t = (f"预期波动 {em.pct:.2f}% > 回本门槛 {need:.2f}%（余量 {margin:+.2f}pp）"
   144	             f"—— 幅度上说得通。")
   145	    else:
   146	        t = (f"预期波动 {em.pct:.2f}% < 回本门槛 {need:.2f}%（差 {-margin:.2f}pp）"
   147	             f"—— 就算方向做对，这类日子的典型幅度也覆盖不了点差和时间损耗。")
   148	    t += (f" 依据：可判定率落在 {em.band} 档（n={em.n}"
   149	          + ("，样本过小，仅作量级参考" if em.weak else "")
   150	          + f"，该档 {em.p_over_1pct:.0%} 的日子波动超过 1%）。")
   151	    if be.theta_share > 0.25:
   152	        t += (f" ⚠️ 这张每天损耗掉权利金的 {be.theta_share:.0%}，"
   153	              f"拿不过夜（{be.dte} 天到期）。")
   154	    return CostVerdict(ok=ok, exp_move=em, need_pct=need, margin=margin, text=t)
   155	
   156	
   157	def candidates(snap, spot: float, direction: str, today: date, *,
   158	               decidable: float, max_dte: int = 45, per_bucket: int = 2,
   159	               fee: float = 3.20, held_days: float = 1.0) -> list[tuple]:
   160	    """顺着信号方向，扫出几张典型合约并算回本门槛。
   161	
   162	    分三个到期桶（≤7 / 8~21 / 22~45 天）各取最接近平值的几张 —— 这样表里
   163	    同时出现"近月便宜但 theta 高"和"远月贵但扛得住"，选择的代价一眼可见。
   164	    用快照盘口（盘前已知），实际下单时点差可能不同。
   165	    """
   166	    want_call = direction in ("看涨", "偏多", "bullish", "up")
   167	    buckets = ((1, 7), (8, 21), (22, max_dte))
   168	    out: list[tuple] = []
   169	    for lo, hi in buckets:
   170	        pool = []
   171	        for c in snap.contracts:
   172	            if c.is_call != want_call:
   173	                continue
   174	            d = (c.expiry - today).days
   175	            if not (lo <= d <= hi):
   176	                continue
   177	            if not (c.bid and c.ask and c.iv and c.iv > 0):
   178	                continue
   179	            # 只看虚值到轻度实值：太深实值资金占用大，太虚 Δ 太小
   180	            m = (c.strike / spot - 1) * (1 if want_call else -1)
   181	            if not (-0.02 <= m <= 0.06):
   182	                continue
   183	            pool.append((abs(m), c))
   184	        pool.sort(key=lambda x: x[0])
   185	        for _, c in pool[:per_bucket]:
   186	            be = breakeven(f"{snap.proxy_symbol}{c.expiry:%y%m%d}{c.kind}{c.strike:g}",
   187	                           spot, c.strike, c.expiry, c.kind, today,
   188	                           c.bid, c.ask, c.iv)
   189	            if be is None or abs(be.delta) < 0.05:
   190	                continue
   191	            out.append((be, judge(be, spot, decidable,
   192	                                  held_days=held_days, fee=fee)))
   193	    return out
     1	"""仓位：按 Kelly 算，不按净资产的固定百分比。
     2	
     3	用户 2026-08-31：「原本我们的守则是仓位管理风险 10%？但是为了这个 10%，
     4	却可能放弃更优的交易，而选择次优，这反而放大了风险。风险管理得再好，
     5	永远在亏损有啥用呢。」
     6	
     7	他说的对，而且这不是主观偏好，是算术：
     8	  净资产 $264 × 10% = $26 —— 买不起任何一张价差（最小占用 $86）。
     9	  能买得起的只有单价 $0.26 以下的期权，而那正是 θ/权利金 >30%、
    10	  点差占比 10~20% 的档位 —— 2026-08-31 那晚四笔全亏，方向基本都对，
    11	  亏在这里。固定百分比对小账户不是风险管理，是【强制选择负期望的工具】。
    12	
    13	【正确的框架】先确认策略正期望，再用 Kelly 定仓位：
    14	    f* = (p·b − q) / b       b = 平均盈利率 / 平均亏损率（都相对占用）
    15	
    16	2026-08-31 实测（回测反推「赢多少、输多少」）：
    17	    策略        胜率   单笔     赢时    输时     盈亏比b   Kelly  1张占用/净资产
    18	    稳健档      82%  +2.84%  +12%  -38.9%   0.31    24%      36%（超配1.5x）
    19	    激进档      63%  +9.99%  +20%   -7.1%   2.84    50%      45%（≈最优）
    20	    顺向买方    23% +17.80% +250%  -51.6%   4.85     7%      44%（超配6x）
    21	
    22	关键在【盈亏比】不在胜率：
    23	  · 稳健档卖得远(墙外2%)，一旦真被打穿就是深度实值，输时亏 38.9%；
    24	  · 激进档买腿保护只在 2.5% 外，破墙也就破一点点，输时只亏 7.1%。
    25	  按平均亏损算耐打程度：稳健档连亏 8 次打光，激进档连亏 32 次。
    26	  真正吃掉小账户的是稳健档，不是激进档 —— 与直觉相反。
    27	
    28	⚠️ Kelly 假设期望估计准确。样本 38~60 笔、阈值在同一批数据上选出，
    29	   估计偏乐观。半 Kelly 是常用折中；但对期权价差，1 组是最小不可分单位，
    30	   往往已经超过半 Kelly —— 那时的选择只有「按 1 组做」或「不做」，
    31	   不存在"缩小到半 Kelly"这个选项。
    32	"""
    33	from __future__ import annotations
    34	
    35	from dataclasses import dataclass
    36	
    37	# 期权价差的最小不可分单位 = 1 组。低于它没有"小一点"这个选项。
    38	MIN_UNIT_NOTE = "期权价差 1 组是最小不可分单位，不存在按比例缩小"
    39	
    40	
    41	@dataclass(frozen=True)
    42	class KellyResult:
    43	    win_rate: float
    44	    win_roi: float          # 赢时相对占用的收益率
    45	    lose_roi: float         # 输时相对占用的亏损率（正数）
    46	    odds: float             # 盈亏比 b = win_roi / lose_roi（平均赚 ÷ 平均亏）
    47	    kelly: float            # f*
    48	    half_kelly: float
    49	    edge: float             # p·b − q，>0 才有优势
    50	
    51	    @property
    52	    def positive_edge(self) -> bool:
    53	        return self.edge > 0
    54	
    55	
    56	def kelly(win_rate: float, per_trade_pct: float, win_roi_pct: float) -> KellyResult:
    57	    """由「胜率 + 单笔期望 + 赢时收益率」反解输时亏损率，再算 Kelly。
    58	
    59	    per_trade_pct = p·win_roi + (1−p)·lose_roi  （都是 %）
    60	    """
    61	    p = max(0.0, min(1.0, win_rate))
    62	    q = 1 - p
    63	    win_roi = win_roi_pct / 100.0
    64	    if q <= 0:
    65	        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
    66	    lose_roi = abs((per_trade_pct / 100.0 - p * win_roi) / q)
    67	    if lose_roi <= 0:
    68	        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
    69	    b = win_roi / lose_roi
    70	    edge = p * b - q
    71	    f = edge / b if b > 0 else 0.0
    72	    return KellyResult(p, win_roi, lose_roi, b, max(0.0, f), max(0.0, f / 2), edge)
    73	
    74	
    75	@dataclass(frozen=True)
    76	class SizeVerdict:
    77	    ok: bool
    78	    n_units: int
    79	    kelly_dollars: float
    80	    unit_occupancy: float
    81	    actual_frac: float      # 实际仓位占净资产
    82	    kelly_frac: float
    83	    over_kelly: float       # 实际 / Kelly，>1 = 超配
    84	    reason: str
    85	
    86	
    87	def size(net_assets: float, unit_occupancy: float, k: KellyResult,
    88	         *, buying_power: float | None = None,
    89	         max_over_kelly: float = 1.5) -> SizeVerdict:
    90	    """给出该下几组。1 组已超 Kelly 时明说超了多少 —— 由人决定要不要下。
    91	
    92	    不静默把仓位压到 0：对小账户，「按 Kelly 缩小」常常等于「不能交易」，
    93	    而不交易就攒不到样本、也赚不到钱。这个取舍要显式交给人。
    94	    """
    95	    if not k.positive_edge:
    96	        return SizeVerdict(False, 0, 0.0, unit_occupancy, 0.0, 0.0, 0.0,
    97	                           f"负优势（p·b−q = {k.edge:+.2f}）—— 这个策略本身不该做，"
    98	                           f"仓位再小也是慢慢亏。")
    99	    if net_assets <= 0 or unit_occupancy <= 0:
   100	        return SizeVerdict(False, 0, 0.0, unit_occupancy, 0.0, k.kelly, 0.0,
   101	                           "净资产或占用无效")
   102	    kd = net_assets * k.kelly
   103	    n = int(kd // unit_occupancy)
   104	    frac1 = unit_occupancy / net_assets
   105	    over = frac1 / k.kelly if k.kelly > 0 else float("inf")
   106	    cap = buying_power if buying_power is not None else net_assets
   107	    if unit_occupancy > cap:
   108	        return SizeVerdict(False, 0, kd, unit_occupancy, frac1, k.kelly, over,
   109	                           f"1 组占用 ${unit_occupancy:.0f} 超过可用 ${cap:.0f} —— "
   110	                           f"做不了。{MIN_UNIT_NOTE}。")
   111	    if n >= 1:
   112	        return SizeVerdict(True, n, kd, unit_occupancy, n * frac1, k.kelly,
   113	                           n * frac1 / k.kelly,
   114	                           f"Kelly ${kd:.0f} → {n} 组（每组 ${unit_occupancy:.0f}）。"
   115	                           f"盈亏比 {k.odds:.2f}、优势 {k.edge:+.2f}。")
   116	    # 1 组已超 Kelly：把超配倍数摆出来，不替人做决定
   117	    lvl = ("可接受" if over <= max_over_kelly else "明显超配")
   118	    return SizeVerdict(True, 1, kd, unit_occupancy, frac1, k.kelly, over,
   119	                       f"Kelly 只允许 ${kd:.0f}，但 1 组要 ${unit_occupancy:.0f}"
   120	                       f"（净资产 {frac1:.0%}，是 Kelly 的 {over:.1f} 倍·{lvl}）。"
   121	                       f"{MIN_UNIT_NOTE} —— 要么按 1 组做并接受这个超配，要么不做。"
   122	                       f"把仓位压到 Kelly 以下的选项不存在。")
   123	
   124	
   125	def ruin_probability(win_rate: float, n_full_loss_to_ruin: int) -> float:
   126	    """连续 n 次全损打光账户的概率上限（假设各次独立、每次都是最坏情形）。"""
   127	    return (1 - win_rate) ** max(1, n_full_loss_to_ruin)
     1	"""墙位卖方价差 —— 把「墙难破」这件事直接做成仓位。
     2	
     3	用户 2026-08-31 提出：「我们目前最可靠的期权结构/墙等数据，最适合的交易策略
     4	应该是卖方价差。因为事实证明破墙很难。」回测证实了这个直觉，但需要三道闸门，
     5	少一道就变成亏损策略。
     6	
     7	═══ 回测（2026-08-31，样本区间 2026-06-25~08-31，7 个品种）═══
     8	
     9	【为什么必须用「近端加总墙」而不是「同到期专属墙」】
    10	用户当时追问「墙的日期也要考虑」——我第一版正是用近端(≤14天)加总的墙去卖
    11	22~45 天的价差，口径错配。但改成同到期专属墙后结果反而崩了：
    12	
    13	  近端加总墙 · 厚度≥10%   45笔  84%胜率  +3.62%/笔  年化 +50%
    14	  近端加总墙 · 厚度≥15%   14笔  86%胜率  +9.73%/笔  年化 +133%
    15	  近端加总墙 · 厚度≥20%    9笔 100%胜率 +11.56%/笔  年化 +147%
    16	  同到期专属墙 · ≥25%     55笔  76%胜率  -5.26%/笔  年化 -72%
    17	  同到期专属墙 · ≥45%     22笔  68%胜率 -16.55%/笔  年化 -208%
    18	
    19	同到期口径提高门槛不但没改善、反而更差，所以这不是幸存者偏差：
    20	**跨到期都堆在同一位置＝真关键位；单个到期的最大 OI＝噪音。**
    21	正确用法是把同到期当【第二重确认】——加总墙的位置上，该到期自己也要有墙：
    22	
    23	  加总≥10% + 同到期≥20% · 15~45天   13笔 92%胜率 +8.93%/笔 年化+98% p=0.003
    24	  加总≥10% + 同到期≥20% ·  4~14天   23笔 78%胜率 +1.15%/笔 年化+47% p=0.011
    25	
    26	【为什么必须是极强信号】亏损全集中在中等信号：
    27	  极强(≥5×)  亏损笔最惨 -24
    28	  中等(2~5×) 亏损笔 -1250 / -499 / -260 —— 大亏 50 倍
    29	【为什么必须看墙的厚度】亏损笔的墙平均 29,969、盈利笔 65,789（差 2.2 倍）；
    30	  墙 OI <30,000 时破墙率 55%，≥60,000 时降到 21%。
    31	  绝对 OI 不可跨品种比较（QQQ 的 4 万 ≠ SLV 的 4 万），故一律用相对占比。
    32	
    33	【到期时间】用户猜「越快到期越不容易破墙」——实测不成立，破墙率与 DTE 无关
    34	（20~29% 横跨 1~45 天）。但胜率随 DTE 单调上升（56%→86%），因为收到的
    35	权利金更厚、缓冲更大。15~45 天单笔收益率最高。
    36	
    37	【铁鹰不成立】双边同时卖，破墙率飙到 57~80%（任一边破就亏）：
    38	  全信号 4~14天 50%胜率 -3.06%/笔　全信号 15~45天 64%胜率 -4.70%/笔
    39	  极强 15~45天 71%胜率 -3.14%/笔 —— 全负。已否决，勿再试。
    40	
    41	⚠️ 所有阈值都是在同一批数据上选出来的，多重比较风险实打实；
    42	   最优组合仅 13 笔。这套参数需要样本外验证才算数。
    43	"""
    44	from __future__ import annotations
    45	
    46	from dataclasses import dataclass
    47	from datetime import date
    48	
    49	MIN_RATIO = 5.0          # 只在极强信号做（中等信号的亏损是极强的 50 倍）
    50	MIN_WALL_SHARE = 0.10    # 加总墙厚度：占该侧近端总 OI 的比例
    51	MIN_EXP_SHARE = 0.20     # 第二重确认：该到期自己的墙也要够厚
    52	WALL_MATCH_TOL = 0.02    # 同到期墙必须落在加总墙 ±2% 内才算「同一位置」
    53	FEE_PER_LEG = 0.80
    54	
    55	# ═══ 三个风险档 ═══════════════════════════════════════════════════════════
    56	# 2026-08-31 完整网格（136 份快照、45 个极强信号、逐日真实盘口重估）。
    57	# 卖腿位置用【相对墙】的偏移：负=墙内（更靠近现价、权利金厚、破墙率高）。
    58	#
    59	# 用户当初那笔 SLV 61P/60P（8/21 开、8/26 到期）正是 aggressive 档：
    60	# 墙在 60、卖 61 = 墙内 1.7%、6 天到期 —— 落在数据上年化最高的区域。
    61	#
    62	# ⚠️ 提前平仓实测【更差】：破墙率只有 5~35%，而平仓要双向吃点差，
    63	#    不如让它到期作废。赚50%平 vs 持有到期：卖墙上 +5% vs +40% 年化。
    64	#    这与"卖方应该收50%就跑"的通行说法相反，但数据如此。
    65	RISK_TIERS = {
    66	    "conservative": {
    67	        "label": "稳健", "offset": 0.02, "width": 0.020, "dte": (15, 45),
    68	        "n": 38, "win_rate": 0.82, "break_rate": 0.11, "per_trade_pct": 2.84,
    69	        "annual_pct": 37, "median_occupancy": 96, "win_roi_pct": 12.0, "worst_pct": -103,
    70	        "note": "破墙率 11% 最低。账户小的时候先活下来。",
    71	    },
    72	    "balanced": {
    73	        "label": "平衡", "offset": 0.0, "width": 0.025, "dte": (15, 45),
    74	        "n": 45, "win_rate": 0.76, "break_rate": 0.20, "per_trade_pct": -0.43,
    75	        "annual_pct": -6, "median_occupancy": 117, "win_roi_pct": 18.0, "worst_pct": -103,
    76	        "note": "卖在墙上。窄宽度下单笔为负——权利金没覆盖住破墙损失。",
    77	    },
    78	    "aggressive": {
    79	        "label": "激进", "offset": -0.02, "width": 0.025, "dte": (4, 14),
    80	        "n": 60, "win_rate": 0.63, "break_rate": 0.35, "per_trade_pct": 9.99,
    81	        "annual_pct": 413, "median_occupancy": 118, "win_roi_pct": 20.0, "worst_pct": -105,
    82	        "note": "年化最高，但胜率仅 63%、破墙 35%。连亏 3 次概率 5.1%，"
    83	                "对小账户是爆仓级风险。",
    84	    },
    85	}
    86	DEFAULT_TIER = "conservative"
    87	
    88	# 兼容旧调用
    89	SELL_OFFSET = RISK_TIERS[DEFAULT_TIER]["offset"]
    90	WIDTH_FRAC = RISK_TIERS[DEFAULT_TIER]["width"]
    91	DTE_MIN, DTE_MAX = RISK_TIERS[DEFAULT_TIER]["dte"]
    92	
    93	BACKTEST = {
    94	    "n": 38, "win_rate": 0.82, "per_trade_pct": 2.84, "annual_pct": 37,
    95	    "p_value": 0.000, "median_occupancy": 96,
    96	    "caveat": "38 笔，阈值在同一批数据上选出，需样本外验证；"
    97	              "年化是「单笔 × 365/持有天数」的外推，未扣信号空窗",
    98	}
    99	
   100	# 卖腿位置 → 权利金比例与破墙率的完整对照（宽5%、15~45天、持有到期）
   101	# 这张表是「为什么不能一味卖近」的凭证：权利金和风险是同一枚硬币。
   102	OFFSET_TRADEOFF = [
   103	    # (偏移, credit/width, 破墙率, 胜率, 单笔%, 年化%)
   104	    (-0.04, 0.28, 0.30, 0.77, 3.20, 44),
   105	    (-0.02, 0.21, 0.21, 0.81, 0.61, 8),
   106	    (0.00, 0.16, 0.20, 0.89, 2.87, 40),
   107	    (0.02, 0.12, 0.09, 0.84, 3.62, 50),
   108	    (0.04, 0.09, 0.05, 0.86, 3.68, 50),
   109	]
   110	
   111	
   112	@dataclass(frozen=True)
   113	class WallSpread:
   114	    kind: str                # 'C' 卖看涨价差 / 'P' 卖看跌价差
   115	    expiry: date
   116	    dte: int
   117	    sell_strike: float
   118	    buy_strike: float
   119	    credit: float            # 每张净收权利金($)，卖腿吃 bid、买腿吃 ask
   120	    width: float
   121	    occupancy: float         # 保证金占用 = (宽度 - 权利金)×100
   122	    wall_strike: float
   123	    wall_share: float        # 加总墙厚度占比
   124	    exp_share: float         # 该到期自己的墙厚度占比
   125	    buffer_pct: float        # 卖腿距现价的缓冲
   126	
   127	    @property
   128	    def max_loss(self) -> float:
   129	        return self.occupancy + FEE_PER_LEG * 4
   130	
   131	    @property
   132	    def roi(self) -> float:
   133	        return self.credit / self.occupancy if self.occupancy > 0 else 0.0
   134	
   135	    @property
   136	    def annual_roi(self) -> float:
   137	        return self.roi * 365 / max(self.dte, 1)
   138	
   139	
   140	@dataclass(frozen=True)
   141	class Verdict:
   142	    ok: bool
   143	    reason: str
   144	    spreads: list[WallSpread]
   145	    gates: dict
   146	
   147	
   148	def _walls_aggregate(snap, obs: date, kind: str, spot: float, max_dte: int = 14):
   149	    """近端加总墙 + 其占该侧总 OI 的比例。"""
   150	    agg: dict[float, int] = {}
   151	    total = 0
   152	    for c in snap.contracts:
   153	        if c.kind != kind:
   154	            continue
   155	        d = (c.expiry - obs).days
   156	        if not (1 <= d <= max_dte):
   157	            continue
   158	        if kind == "C" and c.strike < spot:
   159	            continue
   160	        if kind == "P" and c.strike > spot:
   161	            continue
   162	        if not (spot * 0.85 <= c.strike <= spot * 1.15):
   163	            continue
   164	        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
   165	        total += c.open_interest
   166	    if not agg or total <= 0:
   167	        return None, 0, 0.0
   168	    k, v = max(agg.items(), key=lambda x: x[1])
   169	    return k, v, v / total
   170	
   171	
   172	def _wall_same_expiry(snap, exp: date, kind: str, spot: float):
   173	    agg: dict[float, int] = {}
   174	    total = 0
   175	    for c in snap.contracts:
   176	        if c.expiry != exp or c.kind != kind:
   177	            continue
   178	        if kind == "C" and c.strike < spot:
   179	            continue
   180	        if kind == "P" and c.strike > spot:
   181	            continue
   182	        if not (spot * 0.85 <= c.strike <= spot * 1.15):
   183	            continue
   184	        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
   185	        total += c.open_interest
   186	    if not agg or total <= 0:
   187	        return None, 0, 0.0
   188	    k, v = max(agg.items(), key=lambda x: x[1])
   189	    return k, v, v / total
   190	
   191	
   192	def tier_params(tier: str = DEFAULT_TIER) -> dict:
   193	    """取风险档参数。未知档位直接抛，不静默回落 —— 档位决定的是爆仓风险。"""
   194	    if tier not in RISK_TIERS:
   195	        raise ValueError(f"未知风险档 {tier!r}，可选：{list(RISK_TIERS)}")
   196	    return RISK_TIERS[tier]
   197	
   198	
   199	def propose(snap, obs: date, spot: float, direction: str, ratio: float,
   200	            tier: str = DEFAULT_TIER) -> Verdict:
   201	    """给出墙位卖方价差候选。三道闸门任一不过就不出候选，并说明卡在哪。
   202	
   203	    direction: '看涨'/'看跌' —— 卖【逆向】侧：看涨卖 put 价差、看跌卖 call 价差。
   204	    """
   205	    gates = {"ratio": ratio, "min_ratio": MIN_RATIO}
   206	    if ratio < MIN_RATIO:
   207	        return Verdict(False, (f"压力倍数 {ratio:.1f}× 未达 {MIN_RATIO:g}× —— "
   208	                               f"回测里中等信号(2~5×)的亏损笔达 -1250/-499/-260，"
   209	                               f"是极强信号(最惨 -24)的 50 倍。这道闸门不能松。"),
   210	                       [], gates)
   211	    if direction not in ("看涨", "看跌"):
   212	        return Verdict(False, f"方向不明（{direction}），无从决定卖哪一侧", [], gates)
   213	
   214	    kind = "P" if direction == "看涨" else "C"
   215	    gates["side"] = kind
   216	    wk, woi, wshare = _walls_aggregate(snap, obs, kind, spot)
   217	    gates.update({"wall_strike": wk, "wall_oi": woi, "wall_share": wshare,
   218	                  "min_wall_share": MIN_WALL_SHARE})
   219	    if wk is None or wshare < MIN_WALL_SHARE:
   220	        return Verdict(False, (f"{kind} 侧近端加总墙厚度 "
   221	                               f"{wshare:.0%} < {MIN_WALL_SHARE:.0%} —— "
   222	                               f"回测里薄墙(OI<30,000)的破墙率 55%，厚墙(≥60,000)只有 21%；"
   223	                               f"亏损笔的墙平均只有盈利笔的一半。"),
   224	                       [], gates)
   225	
   226	    tp = tier_params(tier)
   227	    dte_lo, dte_hi = tp["dte"]
   228	    gates["tier"] = tier
   229	    gates["tier_label"] = tp["label"]
   230	    legs_by_exp: dict[date, list] = {}
   231	    for c in snap.contracts:
   232	        if c.kind != kind or not (c.bid and c.ask and c.bid > 0):
   233	            continue
   234	        d = (c.expiry - obs).days
   235	        if dte_lo <= d <= dte_hi:
   236	            legs_by_exp.setdefault(c.expiry, []).append(c)
   237	
   238	    out: list[WallSpread] = []
   239	    skipped: list[str] = []
   240	    for exp in sorted(legs_by_exp):
   241	        ewk, ewoi, eshare = _wall_same_expiry(snap, exp, kind, spot)
   242	        # 第二重确认：该到期自己的墙必须落在加总墙同一位置且够厚
   243	        if ewk is None or abs(ewk - wk) / wk > WALL_MATCH_TOL:
   244	            skipped.append(f"{exp} 该到期的墙在 "
   245	                           f"{ewk:g}（与加总墙 {wk:g} 不同位）" if ewk else f"{exp} 无墙")
   246	            continue
   247	        if eshare < MIN_EXP_SHARE:
   248	            skipped.append(f"{exp} 同到期墙厚度 {eshare:.0%} < {MIN_EXP_SHARE:.0%}")
   249	            continue
   250	        ls = sorted(legs_by_exp[exp], key=lambda c: c.strike)
   251	        off = tp["offset"]
   252	        tgt = wk * (1 + off) if kind == "C" else wk * (1 - off)
   253	        # ⚠️ 墙内偏移不得越过现价：墙本身可能已经很贴近现价，再往内推就成了
   254	        # 卖【实值】腿 —— 那不是收权利金，是直接接货。2026-08-31 实测：
   255	        # GLD 现价 407.23、put 墙 405，墙内 2% 推到 413.1，越过现价 6 美元。
   256	        if kind == "C":
   257	            tgt = max(tgt, spot * 1.001)
   258	            pool = [c for c in ls if c.strike > spot]
   259	        else:
   260	            tgt = min(tgt, spot * 0.999)
   261	            pool = [c for c in ls if c.strike < spot]
   262	        if not pool:
   263	            skipped.append(f"{exp} 无虚值腿可卖")
   264	            continue
   265	        sell = min(pool, key=lambda c: abs(c.strike - tgt))
   266	        wf = tp["width"]
   267	        wt = sell.strike * (1 + wf) if kind == "C" else sell.strike * (1 - wf)
   268	        cands = [c for c in ls if (c.strike > sell.strike if kind == "C"
   269	                                   else c.strike < sell.strike)]
   270	        # 缓冲太薄的直接弃 —— 卖腿贴着现价时权利金再厚也扛不住一天的波动
   271	        if abs(sell.strike / spot - 1) < 0.005:
   272	            skipped.append(f"{exp} 卖腿 {sell.strike:g} 距现价不足 0.5%")
   273	            continue
   274	        if not cands:
   275	            continue
   276	        buy = min(cands, key=lambda c: abs(c.strike - wt))
   277	        credit = (sell.bid - buy.ask) * 100
   278	        width = abs(buy.strike - sell.strike) * 100
   279	        if credit <= 0 or width <= 0:
   280	            continue
   281	        out.append(WallSpread(
   282	            kind=kind, expiry=exp, dte=(exp - obs).days,
   283	            sell_strike=sell.strike, buy_strike=buy.strike,
   284	            credit=credit, width=width, occupancy=width - credit,
   285	            wall_strike=wk, wall_share=wshare, exp_share=eshare,
   286	            buffer_pct=abs(sell.strike / spot - 1) * 100))
   287	    gates["skipped"] = skipped[:5]
   288	    if not out:
   289	        return Verdict(False, (f"加总墙在 {wk:g}（厚度 {wshare:.0%}，已达标），"
   290	                               f"但 {dte_lo}~{dte_hi} 天内没有到期能通过第二重确认"
   291	                               f"（该到期自己的墙需落在同一位置且占比 ≥{MIN_EXP_SHARE:.0%}）。"
   292	                               f"　当前档位 {tp['label']}：{dte_lo}~{dte_hi} 天、"
   293	                               f"卖腿{'墙内' if tp['offset'] < 0 else '墙外'}"
   294	                               f"{abs(tp['offset']) * 100:.0f}%、宽{tp['width'] * 100:.1f}%。"
   295	                               + ("　跳过原因：" + "；".join(skipped[:3]) if skipped else "")),
   296	                       [], gates)
   297	    out.sort(key=lambda s: -s.annual_roi)
   298	    return Verdict(True, (f"压力比 {ratio:.1f}× 过闸，{kind} 侧加总墙 {wk:g} "
   299	                          f"厚度 {wshare:.0%}，{len(out)} 个到期通过同到期确认。"),
   300	                   out, gates)

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "nl -ba undertow/analyze/cost_gate.py; nl -ba undertow/analyze/sizing.py; nl -ba undertow/analyze/gamma.py | sed -n '480,660p'; nl -ba undertow/analyze/flow.py | sed -n '2060,2185p'; nl -ba undertow/analyze/backmonth.py" in /Users/yhdong/Trading
 succeeded in 0ms:
     1	"""成本闸门：把「这天大概能走多少」和「这笔要走多少才不亏」并排放。
     2	
     3	起因（用户 2026-08-31 那晚的四笔亏损）：
     4	  SLV 9/2 到期 60P，买 0.75 卖 0.73。方向是【对】的（SLV 60 破到 59.66），
     5	  但那张 put 需要标的跌 1.9% 才覆盖点差+theta，当天只跌 0.75% → 白做。
     6	  同晚 GLD 410C 持有 1 分钟割在 0.70，两小时后 0.89 —— 那 11% 就是点差本身。
     7	
     8	两个数缺一不可：
     9	  预期波动  = 这类日子标的大概走多少（由可判定率预测，见下表）
    10	  回本门槛  = 你选的这张合约要走多少才不亏（真实 bid/ask + BS 的 Δ/θ）
    11	预期波动 < 回本门槛 → 不用等开盘就知道做不成。
    12	
    13	⚠️ 全部为【事前】量：可判定率来自盘前快照，bid/ask 是下单时的实时盘口。
    14	"""
    15	from __future__ import annotations
    16	
    17	from dataclasses import dataclass
    18	from datetime import date
    19	
    20	from undertow.analyze import blackscholes as bs
    21	
    22	# ─────────────────────────────────────────────────────────────────────────────
    23	# 预期波动：由可判定率预测（2026-08-31 实测，倍数 ≥2× 的 66 笔）
    24	#
    25	#   可判定率      笔数   命中率   当日|波动|   顺向收益   波动>1%比例
    26	#   <35%          12    67%     0.69%      +0.32%      8%
    27	#   35~60%        25    64%     0.48%      +0.24%     12%
    28	#   60~80%        23    61%     0.89%      +0.45%     39%
    29	#   ≥80%           6    83%     1.84%      +1.50%     50%
    30	#
    31	#   可判定率 vs 当日波动幅度  r=+0.243 (n=66, t=2.01, p≈0.048)
    32	#   可判定率 vs 是否命中     r=+0.053  ← 几乎为零
    33	#
    34	# 关键：可判定率预告的不是【方向准不准】（命中率 65% vs 66%，一样），
    35	#       而是【今天有没有幅度】（波动 0.55% vs 1.09%，翻倍）。
    36	#
    37	# ⚠️ 这是在同一批数据上的第二次找模式（先测倍数闸门、再测可判定率），
    38	#    多重比较风险仍在；≥80% 档只有 6 笔。分档均值只能当量级参考，
    39	#    报告必须连同样本量一起显示，不得呈现为点估计。
    40	# ─────────────────────────────────────────────────────────────────────────────
    41	
    42	EXPECTED_MOVE_TABLE = [
    43	    (0.00, 0.35, 0.69, 12, 0.08),
    44	    (0.35, 0.60, 0.48, 25, 0.12),
    45	    (0.60, 0.80, 0.89, 23, 0.39),
    46	    (0.80, 1.01, 1.84,  6, 0.50),
    47	]
    48	EXPECTED_MOVE_EVIDENCE = {
    49	    "n": 66, "r_move": 0.243, "t": 2.01, "p": 0.048, "r_hit": 0.053,
    50	    "note": "同批数据第二次找模式，需样本外验证",
    51	}
    52	
    53	
    54	@dataclass(frozen=True)
    55	class ExpectedMove:
    56	    pct: float          # 该档实测平均 |当日波动|(%)
    57	    n: int              # 该档样本量
    58	    p_over_1pct: float  # 该档里波动 >1% 的比例
    59	    band: str
    60	
    61	    @property
    62	    def weak(self) -> bool:
    63	        return self.n < 10
    64	
    65	
    66	def expected_move(decidable: float) -> ExpectedMove:
    67	    """由可判定率给出这类日子的预期波动幅度（量级参考，非点预测）。"""
    68	    for lo, hi, pct, n, p1 in EXPECTED_MOVE_TABLE:
    69	        if lo <= decidable < hi:
    70	            lab = f"{lo:.0%}~{hi:.0%}" if hi <= 1.0 else f"≥{lo:.0%}"
    71	            return ExpectedMove(pct=pct, n=n, p_over_1pct=p1, band=lab)
    72	    return ExpectedMove(pct=EXPECTED_MOVE_TABLE[-1][2], n=EXPECTED_MOVE_TABLE[-1][3],
    73	                        p_over_1pct=EXPECTED_MOVE_TABLE[-1][4], band="≥80%")
    74	
    75	
    76	@dataclass(frozen=True)
    77	class Breakeven:
    78	    symbol: str
    79	    strike: float
    80	    expiry: date
    81	    kind: str
    82	    dte: int
    83	    bid: float
    84	    ask: float
    85	    iv: float
    86	    delta: float
    87	    theta: float          # 每日（bs.theta 已是每日，不要再除 365）
    88	    spread_pct: float
    89	    cost: float           # 一张成本（含权利金，不含手续费）
    90	
    91	    def need_pct(self, spot: float, held_days: float, fee: float = 0.0) -> float:
    92	        """标的要往顺向走多少 %，这笔才刚好不亏。
    93	
    94	        need = (点差 + |θ|×持有天数 + 手续费/100) / |Δ| / 现价
    95	        """
    96	        if not self.delta or spot <= 0:
    97	            return float("inf")
    98	        spread = self.ask - self.bid
    99	        need = (spread + abs(self.theta) * held_days + fee / 100.0) / abs(self.delta)
   100	        return need / spot * 100.0
   101	
   102	    @property
   103	    def theta_share(self) -> float:
   104	        """每日损耗占权利金的比例 —— 决定这张能拿多久。"""
   105	        mid = (self.ask + self.bid) / 2
   106	        return abs(self.theta) / mid if mid > 0 else float("inf")
   107	
   108	
   109	def breakeven(symbol: str, spot: float, strike: float, expiry: date, kind: str,
   110	              today: date, bid: float, ask: float, iv: float) -> Breakeven | None:
   111	    """用真实盘口 + BS 的 Δ/θ 算这张合约的回本门槛。"""
   112	    if not (bid and ask and ask > 0 and iv and iv > 0):
   113	        return None
   114	    dte = (expiry - today).days
   115	    T = max(dte, 0.5) / 365.0
   116	    k = "C" if kind.upper().startswith("C") else "P"
   117	    dl = bs.delta(spot, strike, T, iv, k)
   118	    th = bs.theta(spot, strike, T, iv, k)   # 已是每日
   119	    mid = (ask + bid) / 2
   120	    return Breakeven(symbol=symbol, strike=strike, expiry=expiry, kind=k, dte=dte,
   121	                     bid=bid, ask=ask, iv=iv, delta=dl, theta=th,
   122	                     spread_pct=(ask - bid) / mid if mid > 0 else float("inf"),
   123	                     cost=ask * 100)
   124	
   125	
   126	@dataclass(frozen=True)
   127	class CostVerdict:
   128	    ok: bool
   129	    exp_move: ExpectedMove
   130	    need_pct: float
   131	    margin: float       # 预期波动 − 回本门槛，正=有余量
   132	    text: str
   133	
   134	
   135	def judge(be: Breakeven, spot: float, decidable: float, *,
   136	          held_days: float = 1.0, fee: float = 3.20) -> CostVerdict:
   137	    """并排对比：这天大概走多少 vs 这张要走多少才不亏。"""
   138	    em = expected_move(decidable)
   139	    need = be.need_pct(spot, held_days, fee)
   140	    margin = em.pct - need
   141	    ok = margin > 0
   142	    if ok:
   143	        t = (f"预期波动 {em.pct:.2f}% > 回本门槛 {need:.2f}%（余量 {margin:+.2f}pp）"
   144	             f"—— 幅度上说得通。")
   145	    else:
   146	        t = (f"预期波动 {em.pct:.2f}% < 回本门槛 {need:.2f}%（差 {-margin:.2f}pp）"
   147	             f"—— 就算方向做对，这类日子的典型幅度也覆盖不了点差和时间损耗。")
   148	    t += (f" 依据：可判定率落在 {em.band} 档（n={em.n}"
   149	          + ("，样本过小，仅作量级参考" if em.weak else "")
   150	          + f"，该档 {em.p_over_1pct:.0%} 的日子波动超过 1%）。")
   151	    if be.theta_share > 0.25:
   152	        t += (f" ⚠️ 这张每天损耗掉权利金的 {be.theta_share:.0%}，"
   153	              f"拿不过夜（{be.dte} 天到期）。")
   154	    return CostVerdict(ok=ok, exp_move=em, need_pct=need, margin=margin, text=t)
   155	
   156	
   157	def candidates(snap, spot: float, direction: str, today: date, *,
   158	               decidable: float, max_dte: int = 45, per_bucket: int = 2,
   159	               fee: float = 3.20, held_days: float = 1.0) -> list[tuple]:
   160	    """顺着信号方向，扫出几张典型合约并算回本门槛。
   161	
   162	    分三个到期桶（≤7 / 8~21 / 22~45 天）各取最接近平值的几张 —— 这样表里
   163	    同时出现"近月便宜但 theta 高"和"远月贵但扛得住"，选择的代价一眼可见。
   164	    用快照盘口（盘前已知），实际下单时点差可能不同。
   165	    """
   166	    want_call = direction in ("看涨", "偏多", "bullish", "up")
   167	    buckets = ((1, 7), (8, 21), (22, max_dte))
   168	    out: list[tuple] = []
   169	    for lo, hi in buckets:
   170	        pool = []
   171	        for c in snap.contracts:
   172	            if c.is_call != want_call:
   173	                continue
   174	            d = (c.expiry - today).days
   175	            if not (lo <= d <= hi):
   176	                continue
   177	            if not (c.bid and c.ask and c.iv and c.iv > 0):
   178	                continue
   179	            # 只看虚值到轻度实值：太深实值资金占用大，太虚 Δ 太小
   180	            m = (c.strike / spot - 1) * (1 if want_call else -1)
   181	            if not (-0.02 <= m <= 0.06):
   182	                continue
   183	            pool.append((abs(m), c))
   184	        pool.sort(key=lambda x: x[0])
   185	        for _, c in pool[:per_bucket]:
   186	            be = breakeven(f"{snap.proxy_symbol}{c.expiry:%y%m%d}{c.kind}{c.strike:g}",
   187	                           spot, c.strike, c.expiry, c.kind, today,
   188	                           c.bid, c.ask, c.iv)
   189	            if be is None or abs(be.delta) < 0.05:
   190	                continue
   191	            out.append((be, judge(be, spot, decidable,
   192	                                  held_days=held_days, fee=fee)))
   193	    return out
     1	"""仓位：按 Kelly 算，不按净资产的固定百分比。
     2	
     3	用户 2026-08-31：「原本我们的守则是仓位管理风险 10%？但是为了这个 10%，
     4	却可能放弃更优的交易，而选择次优，这反而放大了风险。风险管理得再好，
     5	永远在亏损有啥用呢。」
     6	
     7	他说的对，而且这不是主观偏好，是算术：
     8	  净资产 $264 × 10% = $26 —— 买不起任何一张价差（最小占用 $86）。
     9	  能买得起的只有单价 $0.26 以下的期权，而那正是 θ/权利金 >30%、
    10	  点差占比 10~20% 的档位 —— 2026-08-31 那晚四笔全亏，方向基本都对，
    11	  亏在这里。固定百分比对小账户不是风险管理，是【强制选择负期望的工具】。
    12	
    13	【正确的框架】先确认策略正期望，再用 Kelly 定仓位：
    14	    f* = (p·b − q) / b       b = 平均盈利率 / 平均亏损率（都相对占用）
    15	
    16	2026-08-31 实测（回测反推「赢多少、输多少」）：
    17	    策略        胜率   单笔     赢时    输时     盈亏比b   Kelly  1张占用/净资产
    18	    稳健档      82%  +2.84%  +12%  -38.9%   0.31    24%      36%（超配1.5x）
    19	    激进档      63%  +9.99%  +20%   -7.1%   2.84    50%      45%（≈最优）
    20	    顺向买方    23% +17.80% +250%  -51.6%   4.85     7%      44%（超配6x）
    21	
    22	关键在【盈亏比】不在胜率：
    23	  · 稳健档卖得远(墙外2%)，一旦真被打穿就是深度实值，输时亏 38.9%；
    24	  · 激进档买腿保护只在 2.5% 外，破墙也就破一点点，输时只亏 7.1%。
    25	  按平均亏损算耐打程度：稳健档连亏 8 次打光，激进档连亏 32 次。
    26	  真正吃掉小账户的是稳健档，不是激进档 —— 与直觉相反。
    27	
    28	⚠️ Kelly 假设期望估计准确。样本 38~60 笔、阈值在同一批数据上选出，
    29	   估计偏乐观。半 Kelly 是常用折中；但对期权价差，1 组是最小不可分单位，
    30	   往往已经超过半 Kelly —— 那时的选择只有「按 1 组做」或「不做」，
    31	   不存在"缩小到半 Kelly"这个选项。
    32	"""
    33	from __future__ import annotations
    34	
    35	from dataclasses import dataclass
    36	
    37	# 期权价差的最小不可分单位 = 1 组。低于它没有"小一点"这个选项。
    38	MIN_UNIT_NOTE = "期权价差 1 组是最小不可分单位，不存在按比例缩小"
    39	
    40	
    41	@dataclass(frozen=True)
    42	class KellyResult:
    43	    win_rate: float
    44	    win_roi: float          # 赢时相对占用的收益率
    45	    lose_roi: float         # 输时相对占用的亏损率（正数）
    46	    odds: float             # 盈亏比 b = win_roi / lose_roi（平均赚 ÷ 平均亏）
    47	    kelly: float            # f*
    48	    half_kelly: float
    49	    edge: float             # p·b − q，>0 才有优势
    50	
    51	    @property
    52	    def positive_edge(self) -> bool:
    53	        return self.edge > 0
    54	
    55	
    56	def kelly(win_rate: float, per_trade_pct: float, win_roi_pct: float) -> KellyResult:
    57	    """由「胜率 + 单笔期望 + 赢时收益率」反解输时亏损率，再算 Kelly。
    58	
    59	    per_trade_pct = p·win_roi + (1−p)·lose_roi  （都是 %）
    60	    """
    61	    p = max(0.0, min(1.0, win_rate))
    62	    q = 1 - p
    63	    win_roi = win_roi_pct / 100.0
    64	    if q <= 0:
    65	        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
    66	    lose_roi = abs((per_trade_pct / 100.0 - p * win_roi) / q)
    67	    if lose_roi <= 0:
    68	        return KellyResult(p, win_roi, 0.0, float("inf"), 1.0, 0.5, float("inf"))
    69	    b = win_roi / lose_roi
    70	    edge = p * b - q
    71	    f = edge / b if b > 0 else 0.0
    72	    return KellyResult(p, win_roi, lose_roi, b, max(0.0, f), max(0.0, f / 2), edge)
    73	
    74	
    75	@dataclass(frozen=True)
    76	class SizeVerdict:
    77	    ok: bool
    78	    n_units: int
    79	    kelly_dollars: float
    80	    unit_occupancy: float
    81	    actual_frac: float      # 实际仓位占净资产
    82	    kelly_frac: float
    83	    over_kelly: float       # 实际 / Kelly，>1 = 超配
    84	    reason: str
    85	
    86	
    87	def size(net_assets: float, unit_occupancy: float, k: KellyResult,
    88	         *, buying_power: float | None = None,
    89	         max_over_kelly: float = 1.5) -> SizeVerdict:
    90	    """给出该下几组。1 组已超 Kelly 时明说超了多少 —— 由人决定要不要下。
    91	
    92	    不静默把仓位压到 0：对小账户，「按 Kelly 缩小」常常等于「不能交易」，
    93	    而不交易就攒不到样本、也赚不到钱。这个取舍要显式交给人。
    94	    """
    95	    if not k.positive_edge:
    96	        return SizeVerdict(False, 0, 0.0, unit_occupancy, 0.0, 0.0, 0.0,
    97	                           f"负优势（p·b−q = {k.edge:+.2f}）—— 这个策略本身不该做，"
    98	                           f"仓位再小也是慢慢亏。")
    99	    if net_assets <= 0 or unit_occupancy <= 0:
   100	        return SizeVerdict(False, 0, 0.0, unit_occupancy, 0.0, k.kelly, 0.0,
   101	                           "净资产或占用无效")
   102	    kd = net_assets * k.kelly
   103	    n = int(kd // unit_occupancy)
   104	    frac1 = unit_occupancy / net_assets
   105	    over = frac1 / k.kelly if k.kelly > 0 else float("inf")
   106	    cap = buying_power if buying_power is not None else net_assets
   107	    if unit_occupancy > cap:
   108	        return SizeVerdict(False, 0, kd, unit_occupancy, frac1, k.kelly, over,
   109	                           f"1 组占用 ${unit_occupancy:.0f} 超过可用 ${cap:.0f} —— "
   110	                           f"做不了。{MIN_UNIT_NOTE}。")
   111	    if n >= 1:
   112	        return SizeVerdict(True, n, kd, unit_occupancy, n * frac1, k.kelly,
   113	                           n * frac1 / k.kelly,
   114	                           f"Kelly ${kd:.0f} → {n} 组（每组 ${unit_occupancy:.0f}）。"
   115	                           f"盈亏比 {k.odds:.2f}、优势 {k.edge:+.2f}。")
   116	    # 1 组已超 Kelly：把超配倍数摆出来，不替人做决定
   117	    lvl = ("可接受" if over <= max_over_kelly else "明显超配")
   118	    return SizeVerdict(True, 1, kd, unit_occupancy, frac1, k.kelly, over,
   119	                       f"Kelly 只允许 ${kd:.0f}，但 1 组要 ${unit_occupancy:.0f}"
   120	                       f"（净资产 {frac1:.0%}，是 Kelly 的 {over:.1f} 倍·{lvl}）。"
   121	                       f"{MIN_UNIT_NOTE} —— 要么按 1 组做并接受这个超配，要么不做。"
   122	                       f"把仓位压到 Kelly 以下的选项不存在。")
   123	
   124	
   125	def ruin_probability(win_rate: float, n_full_loss_to_ruin: int) -> float:
   126	    """连续 n 次全损打光账户的概率上限（假设各次独立、每次都是最坏情形）。"""
   127	    return (1 - win_rate) ** max(1, n_full_loss_to_ruin)
   480	    wall_hi, wall_lo = spot * (1 + WALL_BAND), spot * (1 - WALL_BAND)
   481	    for c in snap.with_oi():
   482	        d = (c.expiry - today).days
   483	        if not (lo <= d <= hi):
   484	            continue
   485	        slot = by_strike.setdefault(c.strike, [0, 0])
   486	        if c.is_call:
   487	            slot[0] += c.open_interest
   488	            tc += c.open_interest
   489	        else:
   490	            slot[1] += c.open_interest
   491	            tp += c.open_interest
   492	    cs = [(s, v[0]) for s, v in by_strike.items() if spot <= s <= wall_hi and v[0] > 0]
   493	    ps = [(s, v[1]) for s, v in by_strike.items() if wall_lo <= s <= spot and v[1] > 0]
   494	    cw, cwo = max(cs, key=lambda x: x[1]) if cs else (spot, 0)
   495	    pw, pwo = max(ps, key=lambda x: x[1]) if ps else (spot, 0)
   496	    return WallLayer(
   497	        key=key, label=label, lo_dte=lo, hi_dte=hi,
   498	        call_wall=cw, call_wall_oi=cwo, put_wall=pw, put_wall_oi=pwo,
   499	        call_walls_top=sorted(cs, key=lambda x: -x[1])[:3],
   500	        put_walls_top=sorted(ps, key=lambda x: -x[1])[:3],
   501	        total_call_oi=tc, total_put_oi=tp, n_strikes=len(by_strike),
   502	    )
   503	
   504	
   505	def layered_walls(snap, today: date, spot: float) -> dict[str, WallLayer]:
   506	    return {k: _layer_walls(snap, today, spot, lo, hi, k, lab)
   507	            for k, lab, lo, hi in WALL_LAYERS}
   508	
   509	
   510	def wall_agreement(layers: dict[str, WallLayer], side: str) -> tuple[bool, str]:
   511	    """近端与中端是否指向同一位置 —— 一致 = 真共识位，不一致 = 混算会造假墙的地方。
   512	
   513	    只判 near vs mid：这两层是交易相关的（本周进出场 + 持仓所在）。far(>45天) 是机构
   514	    长期布局，与本周价格无因果关系，给它否决权会把真共识误报成分歧 —— 实测 2026-08-31
   515	    GLD call 近/中端都是 430、far 在 460，若三层同判就会把 430 这个真阻力抹掉。
   516	    far 仅作为附注呈现，不参与 agree 判定。
   517	    """
   518	    def _w(L):
   519	        return (L.call_wall if side == "call" else L.put_wall,
   520	                L.call_wall_oi if side == "call" else L.put_wall_oi)
   521	    n_w, n_oi = _w(layers["near"])
   522	    m_w, m_oi = _w(layers["mid"])
   523	    f_w, f_oi = _w(layers["far"])
   524	    far_note = f"；远端在 {f_w:g}" if f_oi > 0 else ""
   525	    if n_oi == 0 or m_oi == 0:
   526	        which = "近端" if n_oi == 0 else "中端"
   527	        return False, f"{which}该侧无仓，无法判定一致性{far_note}"
   528	    if n_w > 0 and abs(m_w - n_w) / n_w <= WALL_SAME_TOL:
   529	        return True, f"近/中端一致于 {n_w:g} · 真共识位{far_note}"
   530	    return False, (f"近端 {n_w:g} vs 中端 {m_w:g} 不一致 · 混算会在两者之间造出假墙"
   531	                   f"{far_note}")
   532	
   533	
   534	@dataclass(frozen=True)
   535	class LadderStep:
   536	    strike: float
   537	    oi: int
   538	    share: float          # 占该层该侧总 OI 的比例
   539	    dist_pct: float       # 相对现价，支撑为负、阻力为正
   540	    gap_after: float = 0.0  # 与下一档之间的真空跨度(%)，0=紧邻
   541	    # 该档里【当日到期】的 OI。报告以 obs_day(=快照日前一工作日) 计时，好让 0DTE 不被
   542	    # 当成已过期剔除（当天它们仍在交易），代价是当日到期也被算进"近端支撑"——而它们
   543	    # 今天收盘就消失。2026-08-31 SLV 的看跌增仓 12,072 张里 7,749 张是当日到期，
   544	    # 不拆开看会把一个当天就蒸发的结构读成持续压力。
   545	    expiring: int = 0
   546	
   547	    @property
   548	    def expiring_share(self) -> float:
   549	        return self.expiring / self.oi if self.oi else 0.0
   550	
   551	
   552	def support_ladder(snap, today: date, spot: float, *, side: str = "put",
   553	                   max_dte: int = 14, min_dte: int = 1,
   554	                   min_share: float = 0.03, gap_pct: float = 2.0,
   555	                   expiring_on: date | None = None) -> list[LadderStep]:
   556	    """近端支撑/阻力阶梯 + 真空区。
   557	
   558	    回答的是"价格往下(上)走，一路上有没有东西挡"，混算墙位给不出这个 ——
   559	    2026-08-31 GLD 近端支撑 407/405/404/402/400/396 六档间距 0.2~0.6%，
   560	    但 396→370 之间是 6.3% 真空：守住 396 很厚，破了 396 比白银滑得还快。
   561	    只有逐档列出来才看得见这种"厚一段、然后断崖"的结构。
   562	
   563	    min_share: 低于此占比的档位视为挡不住，不计入阶梯（但计入真空跨度）。
   564	    gap_pct:   相邻两档间距超过此值即标注为真空区。
   565	    """
   566	    want_call = side == "call"
   567	    agg: dict[float, int] = {}
   568	    exp: dict[float, int] = {}
   569	    for c in snap.with_oi():
   570	        if c.is_call != want_call:
   571	            continue
   572	        d = (c.expiry - today).days
   573	        if not (min_dte <= d <= max_dte):
   574	            continue
   575	        if want_call and c.strike <= spot:
   576	            continue
   577	        if not want_call and c.strike >= spot:
   578	            continue
   579	        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
   580	        if expiring_on is not None and c.expiry == expiring_on:
   581	            exp[c.strike] = exp.get(c.strike, 0) + c.open_interest
   582	    total = sum(agg.values())
   583	    if total <= 0:
   584	        return []
   585	    # 支撑从高到低（离现价由近及远）；阻力从低到高
   586	    ordered = sorted(agg.items(), key=lambda x: -x[0] if not want_call else x[0])
   587	    keep = [(k, v) for k, v in ordered if v / total >= min_share]
   588	    out: list[LadderStep] = []
   589	    for i, (k, v) in enumerate(keep):
   590	        gap = 0.0
   591	        if i + 1 < len(keep):
   592	            nxt = keep[i + 1][0]
   593	            g = abs(nxt - k) / k * 100
   594	            if g >= gap_pct:
   595	                gap = g
   596	        out.append(LadderStep(strike=k, oi=v, share=v / total,
   597	                              dist_pct=(k / spot - 1) * 100, gap_after=gap,
   598	                              expiring=exp.get(k, 0)))
   599	    return out
   600	
   601	
   602	def ladder_bands(snap, today: date, spot: float, *, max_dte: int = 14) -> dict:
   603	    """近端下方支撑按跌幅分区的密度 —— 用来一眼看出"支撑堆在脚下还是堆在深渊"。
   604	
   605	    2026-08-31 实测：GLD 0~-5% 占下方 70%，SLV 只占 30%、44% 堆在 -10% 以下。
   606	    这就是"黄金阶梯式支撑、白银支撑稀疏"的量化形式。
   607	    """
   608	    agg: dict[float, int] = {}
   609	    for c in snap.with_oi():
   610	        if c.is_call or c.strike >= spot:
   611	            continue
   612	        if not (1 <= (c.expiry - today).days <= max_dte):
   613	            continue
   614	        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
   615	    total = sum(agg.values())
   616	    if total <= 0:
   617	        return {"total": 0, "bands": []}
   618	    bands = []
   619	    for lo, hi, lab in ((0, 5, "0~-5%"), (5, 10, "-5~-10%"), (10, 100, "-10% 以下")):
   620	        v = sum(x for k, x in agg.items() if lo <= (1 - k / spot) * 100 < hi)
   621	        bands.append({"label": lab, "oi": v, "share": v / total})
   622	    return {"total": total, "bands": bands, "max_dte": max_dte}
  2060	
  2061	    def _zone(legs):
  2062	        buy = sum(c.d_oi for c in legs if c.bias == "bearish")     # put 买方保护
  2063	        sell = sum(c.d_oi for c in legs if c.bias == "bullish")    # put 卖方收权利金
  2064	        strikes = sorted({c.strike for c in legs if abs(c.d_oi) >= MOVE_MIN_DOI})
  2065	        return {"buy_put": buy, "sell_put": sell, "iv": _wiv(legs),
  2066	                "doi": sum(abs(c.d_oi) for c in legs),
  2067	                "strikes": strikes[:4]}
  2068	
  2069	    return {
  2070	        "wall": wall,
  2071	        "wall_pct": ((wall / spot - 1) * 100 if spot else None),
  2072	        "above": _zone(above), "at": _zone(at), "below": _zone(below),
  2073	        # 供人自行比较，不做任何判定
  2074	        "iv_gap_below_minus_at": (
  2075	            round(_wiv(below) - _wiv(at), 2)
  2076	            if (_wiv(below) is not None and _wiv(at) is not None) else None),
  2077	    }
  2078	
  2079	
  2080	
  2081	# ─────────────────────────────────────────────────────────────────────────────
  2082	# 可交易信息闸门（2026-08-31 加）
  2083	#
  2084	# 起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，你又说
  2085	# 这其实是不准的」。查下去发现整体统计不显著是被【横盘日】稀释的：
  2086	#
  2087	#   开火信号 26 笔，按当日实际波动分组
  2088	#     横盘 <0.5%      6/12 =  50%   平均 +0.01%   ← 掷硬币
  2089	#     小动 0.5~1.5%   7/10 =  70%   平均 +0.26%
  2090	#     中动 1.5~3%      3/3 = 100%   平均 +1.85%
  2091	#     大动 >3%         1/1 = 100%   平均 +3.16%
  2092	#
  2093	# 但"今天会不会有行情"事后才知道。所以要找一个【盘前已知】的代理。
  2094	# 测了两族候选（91 品种-日 / 36 日期簇，快照 D 盘前 → 当日 open→close）：
  2095	#
  2096	#   闸门            拦掉  放行  拦掉组胜率  放行组胜率  放行顺向  Fisher p
  2097	#   可判定率 <30%     16   75      38%       61%     +0.37%   0.099
  2098	#   压力倍数 <1.5×    15   76      33%       62%     +0.40%   0.050
  2099	#   压力倍数 <2×      29   62      41%       65%     +0.46%   0.044  ←
  2100	#   压力倍数 <3×      47   44      51%       64%     +0.44%   0.290
  2101	#
  2102	# 选 2×：p 最小，且放行组胜率在 2× 之后稳定在 64~65%（不是单点异常，是拐点）；
  2103	# 2 倍本身也有先验合理性，不是从数据里挖出来的怪阈值。
  2104	#
  2105	# ⚠️ 诚实标注：测了 10 个阈值，Bonferroni 校正后 0.044×10 = 0.44，不再显著。
  2106	#    这个闸门需要样本外验证才算数 —— 报告里必须连同样本量一起显示，
  2107	#    不得呈现为已确证的规则。
  2108	# ⚠️ 拦掉组胜率 41%（低于 50%）不代表可以反着做：n=29，且 41% 与 50% 的差异
  2109	#    本身不显著。正确读法是"这些日子没信息"，不是"这些日子反向有信息"。
  2110	# ─────────────────────────────────────────────────────────────────────────────
  2111	
  2112	TRADEABLE_MIN_RATIO = 2.0     # 压力倍数低于此 → 不出方向结论
  2113	TRADEABLE_MIN_DECIDABLE = 0.30  # 可判定率低于此 → 附加警告（p=0.099，未达显著）
  2114	
  2115	# 闸门的实测表现，报告直接引用，不得只写结论不写样本
  2116	GATE_EVIDENCE = {
  2117	    "n_pairs": 91, "n_clusters": 36,
  2118	    "blocked_n": 29, "blocked_hit": 0.41, "blocked_ret": -0.12,
  2119	    "passed_n": 62, "passed_hit": 0.65, "passed_ret": +0.46,
  2120	    "fisher_p": 0.044, "bonferroni_p": 0.44, "n_thresholds_tested": 10,
  2121	}
  2122	
  2123	
  2124	def tradeable_info(fa) -> dict:
  2125	    """今天这个品种的资金流里有没有可交易信息。
  2126	
  2127	    返回 dict：tradeable(bool) / ratio / decidable / reason / evidence
  2128	    只看【盘前已知】的量（增仓结构），不含任何当日价格信息。
  2129	    """
  2130	    inc = [c for c in fa.changes if c.d_oi > 0]
  2131	    dn = sum(c.d_oi for c in inc if c.bias == "bearish")
  2132	    up = sum(c.d_oi for c in inc if c.bias == "bullish")
  2133	    nu = sum(c.d_oi for c in inc if c.bias == "neutral")
  2134	    total = dn + up + nu
  2135	    if total <= 0:
  2136	        return {"tradeable": False, "ratio": 0.0, "decidable": 0.0,
  2137	                "reason": "无增仓，无从判断", "evidence": GATE_EVIDENCE}
  2138	    decidable = (dn + up) / total
  2139	    hi, lo = max(dn, up), min(dn, up)
  2140	    ratio = hi / lo if lo > 0 else (float("inf") if hi > 0 else 0.0)
  2141	    side = "看跌" if dn > up else ("看涨" if up > dn else "无倾向")
  2142	    if ratio < TRADEABLE_MIN_RATIO:
  2143	        return {"tradeable": False, "ratio": ratio, "decidable": decidable,
  2144	                "side": side,
  2145	                "reason": (f"多空压力比 {ratio:.1f}×，低于 {TRADEABLE_MIN_RATIO:g}× 闸门"
  2146	                           f"（看跌 {dn:,} / 看涨 {up:,}）—— 这不是分歧信号，"
  2147	                           f"是**没有信息**。实测这类日子胜率 "
  2148	                           f"{GATE_EVIDENCE['blocked_hit']:.0%}、"
  2149	                           f"顺向收益 {GATE_EVIDENCE['blocked_ret']:+.2f}%。"),
  2150	                "evidence": GATE_EVIDENCE}
  2151	    warn = ""
  2152	    if decidable < TRADEABLE_MIN_DECIDABLE:
  2153	        warn = (f"　⚠️ 但可判定率仅 {decidable:.0%}（{nu:,}/{total:,} 张判不出买卖方，"
  2154	                f"多因 IV 齐落时买方腿被闸门掐掉）——倍数是在很小的分子分母上算的。")
  2155	    return {"tradeable": True, "ratio": ratio, "decidable": decidable, "side": side,
  2156	            "reason": (f"多空压力比 {ratio:.1f}×（看跌 {dn:,} / 看涨 {up:,}），"
  2157	                       f"过 {TRADEABLE_MIN_RATIO:g}× 闸门。实测这类日子胜率 "
  2158	                       f"{GATE_EVIDENCE['passed_hit']:.0%}、"
  2159	                       f"顺向 {GATE_EVIDENCE['passed_ret']:+.2f}%/笔。{warn}"),
  2160	            "evidence": GATE_EVIDENCE}
  2161	
  2162	
  2163	# ─────────────────────────────────────────────────────────────────────────────
  2164	# R15 · 极端尾部只读结构，不读 IV
  2165	#
  2166	# 深虚合约（~1Δ）绝对价格极小：一个最小跳动即可反推出巨大的 IV 变化，叠加报价
  2167	# 稀疏与曲面拟合误差，「OI↑ + IV↓ = 卖方」那套四象限判定在这里系统性失效。
  2168	# 改看【数量配对结构】：
  2169	#   · 1:1 等量 → 垂直/尾部价差（已由 detect_spreads 覆盖）
  2170	#   · 1:2 倍量 → 比例价差（本函数）：买一份近腿、卖两份远腿
  2171	#   · 跨行权价相对 IV 强弱 → 买入腿相对跑赢（作为佐证，不单独定性）
  2172	#
  2173	# 比例价差的方向含义与等量价差不同：买1卖2 的净卖出使它在标的温和上涨时获利，
  2174	# 但远腿被突破后风险敞开 —— 所以它表达的是「看涨到某个位置为止」，
  2175	# 不是无限看涨。逐腿判定会把它读成「大量卖 call = 强烈看跌」，正好读反。
  2176	# ─────────────────────────────────────────────────────────────────────────────
  2177	
  2178	RATIO_MIN_SIZE = 300         # 近腿最小张数（滤掉零散单）
  2179	RATIO_TOL = 0.25             # 倍数容差：1:2 允许 1:1.75 ~ 1:2.25
  2180	RATIO_MAX_WIDTH_FRAC = 0.15  # 两腿行权价最大间距（相对近腿行权价）
  2181	TAIL_MAX_DELTA = 0.12        # 「极端尾部」判定：|Δ| ≤ 此值时 IV 判定不可信
  2182	
  2183	# ⚠️ 只靠 ΔOI 配对会大量捡到巧合。2026-08-31 零假设检验（把 ΔOI 在行权价之间
  2184	# 随机打乱 20 次，看还能"检出"多少）：
  2185	#     GLD 真实 9 / 随机均值 6.2（1.5x）   SLV 12 / 9.4（1.3x）
     1	"""远月结构异动扫描（playbook R16）—— 近月窗口的已知盲区。
     2	
     3	flow 层只看 ≤45 天 + 近价带，结构性地看不见远月布局：机构在 12 月合约上
     4	连续几日堆 call、而近月 ATM IV 在降、现价上方 call 卖压未撤 —— 报告零提示。
     5	两个时间尺度的仓位并存并不矛盾（一边持远月上行尾部、一边近月做空）。
     6	
     7	【时间尺度隔离纪律】远月异动只作长期背景：
     8	  · 不进综合分、不进日度方向研判、不改任何近月位点
     9	  · 输出必须自带「月度级配置信号，与本周方向无关」标注
    10	  · 与近月压力口径隔离，避免尾部污染 tilt
    11	这条纪律是硬约束，由测试锁住 —— 违反它就等于用季度级的仓位去指导当日交易。
    12	"""
    13	from __future__ import annotations
    14	
    15	from dataclasses import dataclass, field
    16	from datetime import date
    17	
    18	# 触发门槛：单日 OI 增幅 ≥50% 且绝对增量 ≥1,000 手。
    19	# 双门槛缺一不可：只看增幅会让 20→60 张这种噪音刷屏；
    20	# 只看绝对量则漏掉基数小但在快速堆积的新建仓（正是要抓的那种）。
    21	BM_MIN_DTE = 46          # 远月起点：与 gamma.WALL_LAYERS 的 far 层对齐
    22	BM_MIN_GROWTH = 0.50
    23	BM_MIN_DOI = 1000
    24	BM_TOP_N = 12
    25	
    26	
    27	@dataclass(frozen=True)
    28	class BackMonthMove:
    29	    expiry: date
    30	    strike: float
    31	    kind: str
    32	    dte: int
    33	    prev_oi: int
    34	    curr_oi: int
    35	    d_oi: int
    36	    growth: float
    37	    moneyness: float       # (strike/spot-1)，call 为正=虚值
    38	    iv: float
    39	    volume: int
    40	
    41	    @property
    42	    def new_build(self) -> bool:
    43	        """基数极小时的堆积 —— 更像新布局而非加仓。"""
    44	        return self.prev_oi < self.d_oi * 0.5
    45	
    46	    @property
    47	    def label(self) -> str:
    48	        side = "上行" if self.kind == "C" else "下行"
    49	        return f"{side}尾部" if abs(self.moneyness) > 0.15 else f"{side}结构"
    50	
    51	
    52	@dataclass(frozen=True)
    53	class BackMonthScan:
    54	    moves: list[BackMonthMove] = field(default_factory=list)
    55	    total_call_doi: int = 0
    56	    total_put_doi: int = 0
    57	    n_expiries: int = 0
    58	    scanned: int = 0
    59	
    60	    @property
    61	    def empty(self) -> bool:
    62	        return not self.moves
    63	
    64	    @property
    65	    def tilt(self) -> str:
    66	        """仅描述远月这一层的倾向。⚠️ 绝不可用于日度方向研判。"""
    67	        c, p = self.total_call_doi, self.total_put_doi
    68	        if c == 0 and p == 0:
    69	            return "无"
    70	        if c >= p * 2:
    71	            return f"远月上行布局占优（call {c:,} vs put {p:,}）"
    72	        if p >= c * 2:
    73	            return f"远月下行布局占优（put {p:,} vs call {c:,}）"
    74	        return f"两侧并存（call {c:,} / put {p:,}）"
    75	
    76	
    77	def scan(prev, curr, today: date, spot: float, *,
    78	         min_dte: int = BM_MIN_DTE, min_growth: float = BM_MIN_GROWTH,
    79	         min_doi: int = BM_MIN_DOI, top_n: int = BM_TOP_N) -> BackMonthScan:
    80	    """扫描远月（>45 天）单日 OI 异动。
    81	
    82	    只用 OI 结构，不做买卖方判定 —— 远月报价稀疏、深虚合约一个跳动就反推出
    83	    巨大 IV 变化（同 R15 的理由），IV 方向在这里不可信。
    84	    """
    85	    pmap = {(c.expiry, c.strike, c.kind): c.open_interest
    86	            for c in prev.contracts} if prev is not None else {}
    87	    moves: list[BackMonthMove] = []
    88	    scanned = 0
    89	    for c in curr.contracts:
    90	        d = (c.expiry - today).days
    91	        if d < min_dte:
    92	            continue
    93	        scanned += 1
    94	        p_oi = pmap.get((c.expiry, c.strike, c.kind), 0)
    95	        d_oi = c.open_interest - p_oi
    96	        if d_oi < min_doi:
    97	            continue
    98	        growth = (d_oi / p_oi) if p_oi > 0 else float("inf")
    99	        if growth < min_growth:
   100	            continue
   101	        moves.append(BackMonthMove(
   102	            expiry=c.expiry, strike=c.strike, kind=c.kind, dte=d,
   103	            prev_oi=p_oi, curr_oi=c.open_interest, d_oi=d_oi,
   104	            growth=growth, moneyness=(c.strike / spot - 1) if spot > 0 else 0.0,
   105	            iv=c.iv, volume=c.volume))
   106	    moves.sort(key=lambda m: -m.d_oi)
   107	    return BackMonthScan(
   108	        moves=moves[:top_n],
   109	        total_call_doi=sum(m.d_oi for m in moves if m.kind == "C"),
   110	        total_put_doi=sum(m.d_oi for m in moves if m.kind == "P"),
   111	        n_expiries=len({m.expiry for m in moves}),
   112	        scanned=scanned,
   113	    )

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "rg -n \"def px_on|px_on\\(|early|提前平仓|同到期|aggregate|cluster|日期簇|Fisher|fisher|credit_wall\" . --glob '"'!data/reports/**'"' --glob '"'!data/snapshots/**'"' --glob '"'!data/logs/**'"' --glob '"'!article/**'"'" in /Users/yhdong/Trading
 succeeded in 0ms:
./AGENTS.md:308:  买方（付权金）：看**净Δ**与**每日 theta 打平所需波动**，因为要提前平仓；
./scripts/analyze_regime.py:57:def cluster_boot(rows, key, iters=4000):
./scripts/analyze_regime.py:109:        res = cluster_boot(sub, k)
./README.zh-CN.md:74:| `gold` | 黄金 | CFTC Disaggregated COT | GLD | GC=F |
./README.zh-CN.md:75:| `silver` | 白银 | CFTC Disaggregated COT | SLV | SI=F |
./README.zh-CN.md:76:| `wti` | WTI 原油 | CFTC Disaggregated COT | USO | CL=F |
./README.zh-CN.md:106:│   ├── cftc_cot             CFTC COT 持仓（Disaggregated / Legacy）
./SKILL.md:111:- **持仓 COT**：CFTC 官方（免费）。物理商品走 Disaggregated，金融期货（美元指数）走 Legacy。✅ 真实，周频、滞后约 3 天。
./SKILL.md:171:1. **组合期权识别（核心）** —— **同品种、同到期尽量当组合看**：垂直价差（牛市看跌/熊市看涨…）、
./undertow/core/models.py:49:    类别命名沿用 Disaggregated 报告:
./undertow/core/config.py:26:    report: str  # 报告类型，目前支持 "disaggregated_fut"
./undertow/collect/cftc_cot.py:1:"""CFTC COT（持仓报告）数据源 —— Disaggregated 周报。
./undertow/collect/cftc_cot.py:4:数据集: 72hh-3qpy = Disaggregated Futures-Only（物理大宗商品用这个）
./undertow/collect/cftc_cot.py:21:    # Disaggregated（物理大宗商品：金属/能源/农产品）
./undertow/collect/cftc_cot.py:22:    "disaggregated_fut": "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
./undertow/collect/cftc_cot.py:24:    # 美元指数等金融品不在 Disaggregated 里，只能走 Legacy（类别更粗）。
./undertow/collect/cftc_cot.py:28:# —— Disaggregated 字段映射 —— 类别 -> (long字段, short字段, spread字段或None)
./undertow/collect/cftc_cot.py:62:    "disaggregated_fut": (_DISAGG_POSITION_FIELDS, _DISAGG_CHANGE_FIELDS),
./undertow/collect/cftc_cot.py:171:            # 集中度字段两种报告（Disaggregated/Legacy）同名（机构口径：前8大净空%）
./undertow/report/markdown.py:12:# 类别中文名（Disaggregated 口径）
./undertow/report/markdown.py:44:           report_kind: str = "disaggregated_fut") -> str:
./undertow/report/markdown.py:100:    header = "# 持仓情报速览（COT / CFTC Disaggregated + Legacy）\n"
./undertow/collect/README.md:11:| `cftc_cot.py` | COT 持仓：CFTC Socrata（Disaggregated 物理品 / Legacy 金融品如美元指数） | 免费·周五发布·截止当周二 |
./docs/reviews/2026-07-10-codex-gpt5.6-sol.md:53:- 同一行权价的不同到期被合并；
./docs/reviews/2026-07-10-codex-gpt5.6-sol.md:411:- 同到期检查；
./config/instruments.json:2:  "_comment": "Instrument registry. Each instrument declares the data-source keys needed to pull its data. Add new instruments here without touching code. CFTC contract codes verified against publicreporting.cftc.gov Disaggregated report (dataset 72hh-3qpy).",
./config/instruments.json:9:        "report": "disaggregated_fut",
./config/instruments.json:38:        "report": "disaggregated_fut",
./config/instruments.json:67:        "report": "disaggregated_fut",
./config/instruments.json:103:        "note": "ICE美元指数期货=真实DXY。金融期货不在Disaggregated报告，持仓走Legacy(非商业=大投机/商业=套保)。暂无合适的免费期权代理(UUP弱)，故只走持仓+价格+宏观三层。"
./undertow/collect/longbridge_kline.py:106:def aggregate(bars: list[dict], k: int, *, by_session: bool = True) -> list[dict]:
./docs/CHANGELOG_2026-08-29.md:375:- **P1-3** 破墙回测对两组各做 p=0.5 二项检验，答非所问。改 Fisher 精确检验（p=0.3888）。
./undertow/analyze/validation.py:64:    cluster_n: int | None = None   # 日期簇数（跨品种同日相关，簇才是独立样本）
./undertow/analyze/validation.py:102:        if self.cluster_n:
./undertow/analyze/validation.py:103:            s += f"，{self.cluster_n} 个日期簇"
./undertow/analyze/validation.py:120:        cluster_n=None,
./undertow/analyze/validation.py:126:        n=62, hits=40, p_value=0.044, baseline=0.50, cluster_n=30,
./undertow/analyze/validation.py:127:        note="放行组 62 笔 65%、顺向 +0.46%；拦掉组 29 笔 41%、顺向 -0.12%。Fisher p=0.044。",
./undertow/analyze/validation.py:131:        n=83, hits=41, p_value=1.000, baseline=0.51, cluster_n=43,
./undertow/report/html.py:1460:                       ratio_html: str = "", credit_wall_html: str = "") -> str:
./undertow/report/html.py:1501:        f'{credit_wall_html}'
./undertow/report/html.py:1651:    cw = fx.get("credit_wall")
./undertow/report/html.py:1665:    elif fx.get("credit_wall_blocked"):
./undertow/report/html.py:1667:                    f'{_esc(fx["credit_wall_blocked"])}</span>')
./undertow/report/html.py:2232:              f'闸门实测（{ev.get("n_pairs")} 品种-日 / {ev.get("n_clusters")} 个日期簇，'
./undertow/report/html.py:2237:              f'顺向 {ev.get("blocked_ret",0):+.2f}%。Fisher p={ev.get("fisher_p")}。'
./undertow/report/html.py:2406:def render_credit_wall(verdicts: dict, spot: float = 0.0, conv=None,
./undertow/report/html.py:2410:    """墙位卖方价差候选（analyze/credit_wall）—— 紧跟期权结构，因为它直接用墙位下单。
./undertow/report/html.py:2413:    回测证实，但要三道闸门：极强信号(≥5×)、加总墙够厚(≥10%)、同到期二次确认。
./undertow/report/html.py:2418:    from undertow.analyze.credit_wall import RISK_TIERS, OFFSET_TRADEOFF
./undertow/report/html.py:2519:        '⚠️ <b>提前平仓实测更差</b>：破墙率只有 5~35%，而平仓要双向吃点差，'
./undertow/cli.py:39:from undertow.analyze.credit_wall import propose as cw_propose
./undertow/cli.py:64:                          render_credit_wall,
./undertow/cli.py:187:                                report_kind=inst.cot.report if inst.cot else "disaggregated_fut")
./undertow/cli.py:352:            from undertow.analyze.credit_wall import propose as _cwp
./undertow/cli.py:366:                        out["credit_wall_blocked"] = _v.reason[:80]
./undertow/cli.py:368:                    out["credit_wall"] = _cw
./undertow/cli.py:1434:            # —— 墙位卖方价差候选（analyze/credit_wall）——
./undertow/cli.py:1435:            credit_wall_html = ""
./undertow/cli.py:1448:                    credit_wall_html = render_credit_wall(
./undertow/cli.py:1626:                                      credit_wall_html=credit_wall_html)
./scripts/backtest_break_wall.py:42:def fisher_exact(a, b, c, d):
./scripts/backtest_break_wall.py:43:    """2x2 Fisher 精确检验（双尾）—— 纯标准库。
./scripts/backtest_break_wall.py:169:    p = fisher_exact(a, bb, c, d)
./scripts/backtest_break_wall.py:171:    print(f"两组差异的 Fisher 精确检验（双尾）p = {p:.4f}")
./README.md:74:| `gold` | Gold | CFTC Disaggregated COT | GLD | GC=F |
./README.md:75:| `silver` | Silver | CFTC Disaggregated COT | SLV | SI=F |
./README.md:76:| `wti` | WTI crude | CFTC Disaggregated COT | USO | CL=F |
./README.md:106:│   ├── cftc_cot             CFTC COT positioning (Disaggregated / Legacy)
./undertow/analyze/flow.py:204:    """检测到的疑似垂直价差结构（同 C/P、**同到期**、相邻行权价、卖一腿 + 买一腿）。"""
./undertow/analyze/flow.py:206:    expiry: date          # 垂直价差必须同到期；标签回填也按它匹配，防跨月串标签
./undertow/analyze/flow.py:1154:    # —— 先识别【平仓中的价差】：两腿同到期、同 C/P、都在减仓、量级相当 ——
./undertow/analyze/flow.py:1197:                     and b.expiry == s.expiry                          # 垂直价差必须同到期
./undertow/analyze/flow.py:1327:                          verdict="仅当日水平（无可比昨日同到期快照），明日起可出日变化判读")
./undertow/analyze/flow.py:1373:# ⚠️ 2026-08-27 稳健性验证结论（118 品种-日，**38 个日期簇**，按簇 block bootstrap）：
./undertow/analyze/flow.py:1427:        # ⚠️ **必须逐到期拟合**：不同到期的偏斜陡峭度不同（近月更陡），
./undertow/analyze/flow.py:1498:        # · 按到期分组是必须的：不同到期期限结构不同，跨期混算的基准对谁都不成立
./undertow/analyze/flow.py:1500:        # · 但【不能】再按 C/P 拆：那样每条腿只和"同到期同侧"比，
./undertow/analyze/flow.py:2094:# 测了两族候选（91 品种-日 / 36 日期簇，快照 D 盘前 → 当日 open→close）：
./undertow/analyze/flow.py:2096:#   闸门            拦掉  放行  拦掉组胜率  放行组胜率  放行顺向  Fisher p
./undertow/analyze/flow.py:2117:    "n_pairs": 91, "n_clusters": 36,
./undertow/analyze/flow.py:2120:    "fisher_p": 0.044, "bonferroni_p": 0.44, "n_thresholds_tested": 10,
./undertow/analyze/flow.py:2218:    只认新建仓（两腿 d_oi > 0）、同到期、同 C/P、远腿数量约为近腿的两倍。
./scripts/analyze_strength.py:198:def cluster_boot(rows, key, iters=4000):
./scripts/analyze_strength.py:224:    res = cluster_boot(rows, k)
./tests/test_flow.py:255:    """同一行权价的不同到期【不得】合并成一条腿。
./tests/test_flow.py:295:    """不同到期的 IV 不得跨期加权平均后再做日差。
./tests/test_flow.py:390:    · 按到期分组是必须的：不同到期期限结构不同，跨期混算的基准对谁都不成立。
./tests/test_flow.py:391:    · 但再按 C/P 拆，每条腿就只和"同到期同侧"比，**Put-Call skew 被整个消掉**，
./tests/test_flow.py:457:    2026-08-27 稳健性验证结论（118 品种-日、**38 个日期簇**，按簇 block bootstrap）：
./tests/test_flow.py:654:    # 同到期版本必须仍能识别
./undertow/analyze/portfolio.py:9:再把同标的同到期的腿识别成价差结构，最后合成组合级总评。
./undertow/analyze/portfolio.py:727:            adv.append(f"【{lg.name}】已收回约 {captured*100:.0f}% 权金且仍价外，可提前平仓落袋、免尾部风险。")
./undertow/analyze/outlook.py:264:        #   基准(无脑做多) 51%，43 个日期簇、四个变体 binomial p 全 = 1.000。
./undertow/analyze/outlook.py:274:                    f"43 个日期簇 p=1.000 —— 与掷硬币无异，看看即可，别当依据。"),
./undertow/analyze/structure_read.py:180:    ⚠️ **必须限定单一到期。** 不同到期的 IV 期限结构不同，把 60 天窗口内多个到期
./undertow/analyze/technicals.py:69:    ⚠️ 长桥没有 4h 周期，由 1h 聚合（aggregate 从最新往回数分组，
./undertow/analyze/technicals.py:74:        from undertow.collect.longbridge_kline import fetch_bars, aggregate
./undertow/analyze/technicals.py:78:    b4 = aggregate(bars, 4)
./undertow/analyze/credit_wall.py:9:【为什么必须用「近端加总墙」而不是「同到期专属墙」】
./undertow/analyze/credit_wall.py:11:22~45 天的价差，口径错配。但改成同到期专属墙后结果反而崩了：
./undertow/analyze/credit_wall.py:16:  同到期专属墙 · ≥25%     55笔  76%胜率  -5.26%/笔  年化 -72%
./undertow/analyze/credit_wall.py:17:  同到期专属墙 · ≥45%     22笔  68%胜率 -16.55%/笔  年化 -208%
./undertow/analyze/credit_wall.py:19:同到期口径提高门槛不但没改善、反而更差，所以这不是幸存者偏差：
./undertow/analyze/credit_wall.py:21:正确用法是把同到期当【第二重确认】——加总墙的位置上，该到期自己也要有墙：
./undertow/analyze/credit_wall.py:23:  加总≥10% + 同到期≥20% · 15~45天   13笔 92%胜率 +8.93%/笔 年化+98% p=0.003
./undertow/analyze/credit_wall.py:24:  加总≥10% + 同到期≥20% ·  4~14天   23笔 78%胜率 +1.15%/笔 年化+47% p=0.011
./undertow/analyze/credit_wall.py:52:WALL_MATCH_TOL = 0.02    # 同到期墙必须落在加总墙 ±2% 内才算「同一位置」
./undertow/analyze/credit_wall.py:62:# ⚠️ 提前平仓实测【更差】：破墙率只有 5~35%，而平仓要双向吃点差，
./undertow/analyze/credit_wall.py:148:def _walls_aggregate(snap, obs: date, kind: str, spot: float, max_dte: int = 14):
./undertow/analyze/credit_wall.py:216:    wk, woi, wshare = _walls_aggregate(snap, obs, kind, spot)
./undertow/analyze/credit_wall.py:248:            skipped.append(f"{exp} 同到期墙厚度 {eshare:.0%} < {MIN_EXP_SHARE:.0%}")
./undertow/analyze/credit_wall.py:299:                          f"厚度 {wshare:.0%}，{len(out)} 个到期通过同到期确认。"),
./tests/test_position_alert.py:252:    # 日历价差（同行权价、不同到期）
./tests/test_position_alert.py:564:    i_cw = src.index("f'{credit_wall_html}'")
./tests/test_position_alert.py:674:    闸门用盘前已知的压力倍数把无信息日标出来（放行 65% vs 拦掉 41%，Fisher p=0.044）。
./tests/test_position_alert.py:693:    for k in ("n_pairs", "n_clusters", "passed_n", "blocked_n",
./tests/test_position_alert.py:694:              "fisher_p", "bonferroni_p", "n_thresholds_tested"):
./tests/test_position_alert.py:873:def test_credit_wall_never_sells_itm():
./tests/test_position_alert.py:882:    from undertow.analyze.credit_wall import (propose, RISK_TIERS, tier_params,
./tests/test_position_alert.py:885:           / "credit_wall.py").read_text("utf-8")
./tests/test_position_alert.py:906:    # 提前平仓的负面结论必须留在代码里，防止日后又"优化"成收50%就跑
./tests/test_position_alert.py:907:    assert "提前平仓实测【更差】" in src
./tests/test_position_alert.py:928:    print("PASS test_credit_wall_never_sells_itm")
./tests/test_position_alert.py:948:    from undertow.analyze.credit_wall import RISK_TIERS
./undertow/analyze/healthcheck.py:26:MIN_NET_DELTA_DEBIT = 0.10   # 借方结构净Δ下限：低于此＝对标的移动几乎无反应（提前平仓打法下无意义）
./undertow/analyze/healthcheck.py:204:    **为什么重要**：在【提前平仓】的打法下（不持有到期、不追求越过行权价），
./undertow/analyze/healthcheck.py:223:      · 买方付权金 → theta 是敌人，必须**提前平仓** → 看【净Δ 与每日损耗】
./undertow/analyze/healthcheck.py:312:            # 买方主闸门＝净Δ + 每日损耗（提前平仓框架）；到期概率仅作次要参考
./undertow/analyze/healthcheck.py:321:                                f"必须提前平仓；这个消耗速度下拖不起。"),
./undertow/analyze/healthcheck.py:332:                            f"注意：买方通常提前平仓，到期口径仅供参考，主看净Δ与每日损耗。"),
./undertow/analyze/healthcheck.py:344:                            f"${abs(nd)*CONTRACT_MULT:.0f}。两腿相互抵消，**在『提前平仓』的打法下**"
./undertow/analyze/expiry_ladder.py:7:  不同到期日细节可能大不相同：当周周度可能全是卖方写权做压制，月度大到期却是买方囤 Call。
./undertow/analyze/position_alert.py:110:    ⚠️ 只处理「同到期、同 C/P、恰好两腿、张数绝对值相等、一买一卖」的
./undertow/analyze/stretch.py:111:        "临界结果（|t| 2.0~2.5）尤其不稳，应按日期做 cluster 稳健标准误后再下结论。",
./tests/test_technicals.py:69:def test_aggregate_must_not_cross_sessions():
./tests/test_technicals.py:80:    from undertow.collect.longbridge_kline import aggregate
./tests/test_technicals.py:90:    agg = aggregate(bars, 4)
./tests/test_technicals.py:100:    print("PASS test_aggregate_must_not_cross_sessions")

hook: PostToolUse
hook: PostToolUse Completed
codex
第一轮已经发现一个高危聚集点：这条链把“同一信号下的多个到期”按独立交易计入样本，又把这些乐观样本直接喂给 Kelly；而报告层在某些超 Kelly 情形仍给出可执行的 1 组建议。我正在核对它是否还叠加了购买力、手续费和结算日口径错误。
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "nl -ba undertow/report/html.py | sed -n '2190,2260p;2390,2545p'; nl -ba undertow/cli.py | sed -n '1380,1470p'; nl -ba tests/test_position_alert.py | sed -n '830,995p'; git show --format=fuller --stat bb22ef6; git show bb22ef6 -- undertow/analyze/credit_wall.py | sed -n '1,380p'" in /Users/yhdong/Trading
 succeeded in 0ms:
  2190	    bl = ""
  2191	    if getattr(ga, "blended_put_wall_oi", 0) > 0:
  2192	        bl = (f'<div class="sub" style="margin-top:8px">对照：旧的 45 天混算口径给 '
  2193	              f'put 墙 {_p(ga.blended_put_wall)}（{ga.blended_put_wall_oi:,}）· '
  2194	              f'call 墙 {_p(ga.blended_call_wall)}（{ga.blended_call_wall_oi:,}）。'
  2195	              f'混算把明天到期和 45 天后到期等权相加，若与上表近端不一致，说明它给的是'
  2196	              f'各层加总出来的位置，实盘那里并没有那么厚的墙。</div>')
  2197	
  2198	    return (
  2199	        '<div class="card"><h2>① 期权结构 · 按到期分层（近端置顶）</h2>'
  2200	        f'<div class="sub">主墙位取<b>近端 ≤14天</b>（{basis}）—— 方向判断问的是"这周会不会破位"，'
  2201	        '跨月加总的墙答不了这个问题。中端是多数持仓所在，远端只作背景，不参与本周判断。</div>'
  2202	        '<table style="margin-top:8px"><tr><th>到期层</th><th>put 墙（下方支撑）</th>'
  2203	        '<th>call 墙（上方阻力）</th><th>该层持仓</th></tr>'
  2204	        + "".join(rows) + '</table>'
  2205	        + agree_html + ladder_html + bands_html + bl
  2206	        + '<div class="sub" style="margin-top:10px">墙 = 该层该侧 OI 最大的行权价（现价 ±15% 内）。'
  2207	          'OI 是持仓不是方向，厚墙只说明"很多人在这有仓"，不等于价格一定停在那。</div></div>'
  2208	    )
  2209	
  2210	
  2211	def render_tradeable_gate(ti, display_name: str = "") -> str:
  2212	    """可交易信息闸门横幅 —— 放在报告最顶，决定下面所有方向结论该给多少信任。
  2213	
  2214	    起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，
  2215	    你又说这其实是不准的」。横盘日信号胜率 50%（掷硬币），有行情日 70~100%，
  2216	    整体不显著是被横盘日稀释的。这个闸门用盘前已知的压力倍数把那类日子标出来。
  2217	    """
  2218	    if not ti:
  2219	        return ""
  2220	    ev = ti.get("evidence", {})
  2221	    ok = ti.get("tradeable")
  2222	    ratio = ti.get("ratio", 0.0)
  2223	    rtxt = "∞" if ratio == float("inf") else f"{ratio:.1f}×"
  2224	    if ok:
  2225	        bg, bd, fg, icon, head = "#1a7f370d", "#1a7f37", "#1a7f37", "✅", "今天有可交易信息"
  2226	    else:
  2227	        bg, bd, fg, icon, head = "#bc4c000d", "#bc4c00", "#bc4c00", "⛔", "今天没有可交易信息"
  2228	    # 证据行：样本量必须和结论同时出现，不得只写结论
  2229	    eb = ""
  2230	    if ev:
  2231	        eb = (f'<div class="sub" style="margin-top:8px;line-height:1.7">'
  2232	              f'闸门实测（{ev.get("n_pairs")} 品种-日 / {ev.get("n_clusters")} 个日期簇，'
  2233	              f'快照盘前 → 当日开盘→收盘）：'
  2234	              f'<b>放行组 {ev.get("passed_n")} 笔胜率 {ev.get("passed_hit",0):.0%}、'
  2235	              f'顺向 {ev.get("passed_ret",0):+.2f}%/笔</b>；'
  2236	              f'拦掉组 {ev.get("blocked_n")} 笔胜率 {ev.get("blocked_hit",0):.0%}、'
  2237	              f'顺向 {ev.get("blocked_ret",0):+.2f}%。Fisher p={ev.get("fisher_p")}。'
  2238	              f'<br><span style="color:#bc4c00">⚠️ 共测了 {ev.get("n_thresholds_tested")} 个阈值，'
  2239	              f'Bonferroni 校正后 p={ev.get("bonferroni_p")} 不再显著 —— '
  2240	              f'这个闸门尚待样本外验证，不是已确证的规则。</span>'
  2241	              f'　拦掉组 41% 不代表可以反着做（n=29，与 50% 的差异本身不显著），'
  2242	              f'正确读法是「这些日子没信息」。</div>')
  2243	    return (f'<div class="card" style="background:{bg};border-left:4px solid {bd}">'
  2244	            f'<h2 style="margin-top:0;color:{fg}">{icon} {_esc(head)}'
  2245	            f'{" · " + _esc(display_name) if display_name else ""}</h2>'
  2246	            f'<div style="font-size:15px;margin:6px 0"><b>多空压力比 {rtxt}</b>'
  2247	            f'　·　可判定率 {ti.get("decidable",0):.0%}'
  2248	            f'　·　倾向 {_esc(ti.get("side","—"))}</div>'
  2249	            f'<div class="sub" style="line-height:1.7">{_esc(ti.get("reason",""))}</div>'
  2250	            f'{eb}</div>')
  2251	
  2252	
  2253	def render_cost_gate(rows, exp_move=None, spot: float = 0.0, side: str = "",
  2254	                     conv=None, unit: str = "", etf_symbol: str = "") -> str:
  2255	    """成本闸门：这天大概走多少 vs 这张要走多少才不亏，并排放。
  2256	
  2257	    起因（用户 2026-08-31 那晚四笔全亏，方向却基本都对）：
  2258	      SLV 9/2 60P 需标的跌 1.9% 才回本，当天只跌 0.75% → 方向对也白做。
  2259	      GLD 410C 持有 1 分钟割掉亏 11%，那 11% 就是点差本身。
  2260	    只给方向不给这两个数，等于让人拿着对的判断去买错的合约。
  2390	            f'<div class="sub">{_esc(x.detail)}</div></div>')
  2391	    return (
  2392	        '<div class="card"><h2>比例价差（1:2）· 逐腿会读反的结构</h2>'
  2393	        '<div class="sub" style="line-height:1.7">买一份近腿、卖两份远腿。它表达的是'
  2394	        '<b>「走到某个位置为止」</b>，不是无限看涨/看跌——而逐腿判定会把卖出的'
  2395	        '两份读成强烈反向压力，方向正好读反。深虚合约还有第二重问题：绝对价格极小，'
  2396	        '一个最小跳动就反推出巨大 IV 变化，「OI↑+IV↓=卖方」在那里系统性失效，'
  2397	        '只能读数量结构。</div>'
  2398	        + "".join(items) +
  2399	        '<div class="sub" style="margin-top:8px;line-height:1.7">'
  2400	        '⚠️ 检出依据是 <b>ΔOI 与当日成交量同时配成 1:2</b>。只用 ΔOI 会大量捡到巧合：'
  2401	        '2026-08-31 零假设检验（随机打乱 ΔOI 后重跑）显示纯 ΔOI 口径的真实/随机比只有 '
  2402	        '1.0~1.5x（TQQQ 是 1.0x，等于全是巧合）；加成交量确认后升到 2.9~6.2x。'
  2403	        '即便如此仍有残余噪音（QQQ 随机基线约 4.5 个），条目数不多时才可信。</div></div>')
  2404	
  2405	
  2406	def render_credit_wall(verdicts: dict, spot: float = 0.0, conv=None,
  2407	                       unit: str = "", etf_symbol: str = "",
  2408	                       buying_power: float | None = None,
  2409	                       net_assets: float | None = None) -> str:
  2410	    """墙位卖方价差候选（analyze/credit_wall）—— 紧跟期权结构，因为它直接用墙位下单。
  2411	
  2412	    用户 2026-08-31：「事实证明破墙很难，我们根据墙进行卖方价差，胜率应该很高。」
  2413	    回测证实，但要三道闸门：极强信号(≥5×)、加总墙够厚(≥10%)、同到期二次确认。
  2414	
  2415	    仓位不作为筛选条件（用户同日：「如果因为仓位选择了更差的交易，反而得不偿失」）：
  2416	    三档全列，占用标出来，选哪档由人定。
  2417	    """
  2418	    from undertow.analyze.credit_wall import RISK_TIERS, OFFSET_TRADEOFF
  2419	    if not verdicts:
  2420	        return ""
  2421	    sym = _esc(etf_symbol)
  2422	
  2423	    def _p(v):
  2424	        return f"{(conv(v) if conv else v):.1f}{_esc(unit)}"
  2425	
  2426	    blocks = []
  2427	    any_ok = False
  2428	    for key in ("conservative", "balanced", "aggressive"):
  2429	        v = verdicts.get(key)
  2430	        if v is None:
  2431	            continue
  2432	        t = RISK_TIERS[key]
  2433	        col = {"conservative": "#1a7f37", "balanced": "#8250df",
  2434	               "aggressive": "#cf222e"}[key]
  2435	        ev = (f'<span class="sub">实测 {t["n"]} 笔 · 胜率 {t["win_rate"]:.0%} · '
  2436	              f'破墙 {t["break_rate"]:.0%} · 单笔 {t["per_trade_pct"]:+.2f}% · '
  2437	              f'年化 {t["annual_pct"]:+.0f}% · 占用中位 ${t["median_occupancy"]:.0f}</span>')
  2438	        # 仓位按 Kelly 判，不按净资产固定百分比 —— 用户 2026-08-31：
  2439	        # 「为了这个 10%，却可能放弃更优的交易而选择次优，这反而放大了风险」
  2440	        kel = ""
  2441	        if net_assets and v.ok and v.spreads:
  2442	            try:
  2443	                from undertow.analyze.sizing import kelly as _kel, size as _sz, ruin_probability
  2444	                _k = _kel(t["win_rate"], t["per_trade_pct"], t.get("win_roi_pct", 12.0))
  2445	                _occ = v.spreads[0].occupancy
  2446	                _v2 = _sz(net_assets, _occ, _k, buying_power=buying_power)
  2447	                import math as _m
  2448	                _n_ruin = max(1, _m.ceil(net_assets / _occ))
  2449	                _rp = ruin_probability(t["win_rate"], _n_ruin)
  2450	                _c = "#1a7f37" if _v2.over_kelly <= 1.5 else "#bc4c00"
  2451	                kel = (f'<div style="margin:5px 0;padding:6px 9px;background:#f6f8fa;'
  2452	                       f'border-radius:5px;font-size:12.5px">'
  2453	                       f'<b>仓位</b>　盈亏比 {_k.odds:.2f}　Kelly {_k.kelly:.0%}'
  2454	                       f'（${net_assets * _k.kelly:.0f}）　'
  2455	                       f'<span style="color:{_c}">1 组 ${_occ:.0f} = 净资产 '
  2456	                       f'{_occ / net_assets:.0%}，Kelly 的 {_v2.over_kelly:.1f} 倍</span>'
  2457	                       f'　·　连续 {_n_ruin} 次全损打光的概率上限 '
  2458	                       f'<b>{_rp:.2%}</b></div>')
  2459	            except Exception:
  2460	                kel = ""
  2461	        cfg = (f'<span class="sub">卖腿{"墙内" if t["offset"] < 0 else "墙外"}'
  2462	               f'{abs(t["offset"]) * 100:.0f}% · 宽 {t["width"] * 100:.1f}% · '
  2463	               f'{t["dte"][0]}~{t["dte"][1]} 天</span>')
  2464	        if not v.ok:
  2465	            blocks.append(
  2466	                f'<div style="border-left:3px solid #d0d7de;padding-left:10px;margin:10px 0">'
  2467	                f'<div style="font-weight:700;color:#57606a">{_esc(t["label"])}档 ⛔ 无候选</div>'
  2468	                f'{cfg}<div class="sub">{_esc(v.reason)}</div></div>')
  2469	            continue
  2470	        any_ok = True
  2471	        rows = []
  2472	        for sp in v.spreads[:3]:
  2473	            afford = ("" if buying_power is None else
  2474	                      ('<span style="color:#1a7f37">✓可下</span>'
  2475	                       if sp.occupancy <= buying_power
  2476	                       else f'<span style="color:#bc4c00">需${sp.occupancy:.0f}</span>'))
  2477	            rows.append(
  2478	                f'<tr><td><b>卖 {_p(sp.sell_strike)}{sp.kind}</b> / 买 {_p(sp.buy_strike)}{sp.kind}'
  2479	                + (f' <span style="color:#0969da">{sym}{sp.sell_strike:g}/{sp.buy_strike:g}</span>'
  2480	                   if conv else "")
  2481	                + f'</td><td class="sub">{sp.expiry}<br>{sp.dte}天</td>'
  2482	                f'<td class="r">+${sp.credit:.0f}</td>'
  2483	                f'<td class="r">${sp.occupancy:.0f}</td>'
  2484	                f'<td class="r">{sp.roi * 100:.0f}%</td>'
  2485	                f'<td class="r">{sp.buffer_pct:.1f}%</td>'
  2486	                f'<td class="r">-${sp.max_loss:.0f}</td>'
  2487	                f'<td>{afford}</td></tr>')
  2488	        blocks.append(
  2489	            f'<div style="border-left:3px solid {col};padding-left:10px;margin:12px 0">'
  2490	            f'<div style="font-weight:700;color:{col}">{_esc(t["label"])}档</div>'
  2491	            f'{cfg}<br>{ev}{kel}'
  2492	            f'<div class="sub" style="margin:4px 0">{_esc(t["note"])}</div>' 
  2493	            '<table style="margin-top:6px"><tr><th>腿位</th><th>到期</th><th>收权利金</th>'
  2494	            '<th>占用</th><th>ROI</th><th>缓冲</th><th>最大亏</th><th></th></tr>'
  2495	            + "".join(rows) + '</table></div>')
  2496	
  2497	    trade = "".join(
  2498	        f'<tr><td>{"墙内" if o < 0 else ("墙上" if o == 0 else "墙外")}'
  2499	        f'{abs(o) * 100:.0f}%</td><td class="r">{cw:.0%}</td><td class="r">{br:.0%}</td>'
  2500	        f'<td class="r">{wr:.0%}</td><td class="r">{pt:+.2f}%</td>'
  2501	        f'<td class="r">{an:+.0f}%</td></tr>'
  2502	        for o, cw, br, wr, pt, an in OFFSET_TRADEOFF)
  2503	
  2504	    head = ("✅ 今日有卖方价差候选" if any_ok else "⛔ 今日无卖方价差候选")
  2505	    return (
  2506	        '<div class="card"><h2>② 墙位卖方价差 · 候选腿位</h2>'
  2507	        f'<div style="font-size:15px;margin:6px 0"><b>{head}</b></div>'
  2508	        '<div class="sub" style="line-height:1.7">三道闸门：极强信号（压力比 ≥5×）、'
  2509	        '近端加总墙厚度 ≥10%、该到期自己的墙落在同一位置且 ≥20%。'
  2510	        '中等信号(2~5×)的亏损笔达 -1250/-499/-260，是极强信号(最惨 -24)的 50 倍，'
  2511	        '所以第一道闸门不能松。</div>'
  2512	        + "".join(blocks) +
  2513	        '<div style="margin-top:14px"><b>卖腿位置的权衡</b>'
  2514	        '<div class="sub">权利金和风险是同一枚硬币 —— 这张表是「不能一味卖近」的凭证</div>'
  2515	        '<table style="margin-top:6px"><tr><th>卖腿位置</th><th>权利金/宽度</th>'
  2516	        '<th>破墙率</th><th>胜率</th><th>单笔</th><th>年化</th></tr>'
  2517	        + trade + '</table></div>'
  2518	        '<div class="sub" style="margin-top:10px;line-height:1.7">'
  2519	        '⚠️ <b>提前平仓实测更差</b>：破墙率只有 5~35%，而平仓要双向吃点差，'
  2520	        '不如让它到期作废（卖墙上：持有到期 +40% vs 赚50%平 +5%）。'
  2521	        '这与「卖方收 50% 就跑」的通行说法相反，但数据如此。<br>'
  2522	        '⚠️ 所有阈值在同一批数据（2026-06-25~08-31）上选出，多重比较风险实打实；'
  2523	        '年化是「单笔 × 365/持有天数」的外推，未扣信号空窗期。'
  2524	        '真实表现一定比表里的数字差。<br>'
  2525	        '⚠️ <b>仓位按 Kelly 判，不按净资产固定百分比。</b>'
  2526	        '净资产 $264 × 10% = $26，买不起任何一张价差（最小占用 $86）——'
  2527	        '固定百分比对小账户不是风险管理，是强制你去选负期望的廉价合约。'
  2528	        '期权价差 1 组是最小不可分单位，超过 Kelly 时只有「按 1 组做」或「不做」，'
  2529	        '没有第三个选项。<br>'
  2530	        '⚠️ 到期日若落在卖腿与买腿之间（如卖61/买60、标的收在 60.5），'
  2531	        '卖腿被行权需接货、买腿保护不到，长桥规则是保证金不足即强平（不会负债），'
  2532	        '但强平时机与价格不由你控制。</div></div>')
  2533	
  2534	
  2535	def render_wall_zones(mg: dict) -> str:
  2536	    """put 墙附近三个区域的原始读数 —— **只摆事实，不下结论**（codex 2026-08-29 P0）。
  2537	
  2538	    上一版在 index 里输出「保护向墙下搬家 / 在给跌破定价 / 下一道防线」+ 红色高亮，
  2539	    用的还是那个回测 30 次只中 1 次、已决定不上线的预测器的门槛 —— 换个名字上线而已。
  2540	    现在不分类、不预测、不用方向色。
  2541	
  2542	    2026-08-31 从 index 移到品种报告（用户：「index 里现在信息太过复杂」）：
  2543	    它是研究性内容，不该占索引页的位置，但也不该消失。
  2544	    """
  2545	    if not mg or not (mg.get("at") or mg.get("below")):
  1380	                    iv_ser = vol_src.fetch_series(inst.vol_index, use_cache=not args.no_cache)
  1381	                    recent = [v for _, v in sorted(iv_ser)[-252:]]
  1382	                    mean_ref = sum(recent) / len(recent) if recent else None
  1383	                    vol_svg = viz.vol_history_svg(
  1384	                        iv_ser, title=f"波动率指数 {inst.vol_index} 近1年（年化 IV，pp）",
  1385	                        mean_ref=mean_ref)
  1386	                    # ⚠️ 回放绝不得写正式台账（codex 2026-08-29 P0：实测跑一次
  1387	                    # --as-of 就改写了 outlook_scores.json 与 5 个 resonance/*.json）
  1388	                    if not replay:
  1389	                        px_ser = px_src.fetch_series(inst, use_cache=not args.no_cache)
  1390	                        _persist_vrp(inst.key, assess_vrp_history(
  1391	                            iv_series=iv_ser, px_dates=px_ser.dates,
  1392	                            px_closes=px_ser.closes, index_name=inst.vol_index))
  1393	                except Exception as ve:
  1394	                    print(f"[提示] {inst.key} 波动率历史跳过: {type(ve).__name__}: {ve}",
  1395	                          file=sys.stderr)
  1396	            vol_analysis_html = render_vol_analysis_section(vr, vol_svg)
  1397	            # —— 铁鹰策略子模块 + 策略统筹（多子模块调度）——
  1398	            condor_plan = assess_condor(snap=curr, vr=vr, today=today, fa=fa)
  1399	            cs_plan = assess_credit_spread(snap=curr, vr=vr, outlook=outlook, today=today, fa=fa)
  1400	            strategy_props = assemble_strategies(directional=plan, condor=condor_plan,
  1401	                                                 credit_spread=cs_plan)
  1402	            strategy_html = (render_strategy_hub(strategy_props) + strategy_html
  1403	                             + render_credit_spread_section(cs_plan)
  1404	                             + render_condor_section(condor_plan))
  1405	            # —— 可交易信息闸门（压力倍数 <2× = 今天没信息，见 flow.tradeable_info）——
  1406	            gate_html = ""
  1407	            try:
  1408	                _ti = tradeable_info(fa)
  1409	                gate_html = render_tradeable_gate(_ti, inst.display_name)
  1410	            except Exception as e:
  1411	                print(f"⚠️ {inst.key} 可交易闸门失败：{type(e).__name__}: {e}", file=sys.stderr)
  1412	
  1413	            # —— 比例价差（playbook R15）：逐腿判定会读反的结构 ——
  1414	            ratio_html = ""
  1415	            try:
  1416	                _rs = detect_ratio_spreads(fa.changes, curr.spot)
  1417	                ratio_html = render_ratio_spreads(
  1418	                    _rs, conv=(ga.to_commodity if ratio is not None else None),
  1419	                    etf_symbol=inst.options.symbol)
  1420	            except Exception as e:
  1421	                print(f"⚠️ {inst.key} 比例价差检测失败：{type(e).__name__}: {e}", file=sys.stderr)
  1422	
  1423	            # —— 远月结构异动（playbook R16）：近月窗口的盲区，只作长期背景 ——
  1424	            backmonth_html = ""
  1425	            try:
  1426	                _bm = backmonth_scan(prev, curr, obs_day, curr.spot)
  1427	                backmonth_html = render_backmonth(
  1428	                    _bm, spot=curr.spot,
  1429	                    conv=(ga.to_commodity if ratio is not None else None),
  1430	                    etf_symbol=inst.options.symbol)
  1431	            except Exception as e:
  1432	                print(f"⚠️ {inst.key} 远月扫描失败：{type(e).__name__}: {e}", file=sys.stderr)
  1433	
  1434	            # —— 墙位卖方价差候选（analyze/credit_wall）——
  1435	            credit_wall_html = ""
  1436	            try:
  1437	                if _ti and _ti.get("side") in ("看涨", "看跌"):
  1438	                    _bp = _na = None
  1439	                    try:
  1440	                        from undertow.collect.longbridge_account import fetch_assets
  1441	                        _a = fetch_assets()
  1442	                        _bp, _na = _a.buy_power, _a.net_assets
  1443	                    except Exception:
  1444	                        pass
  1445	                    _vs = {t: cw_propose(curr, obs_day, curr.spot, _ti["side"],
  1446	                                         _ti["ratio"], tier=t)
  1447	                           for t in ("conservative", "balanced", "aggressive")}
  1448	                    credit_wall_html = render_credit_wall(
  1449	                        _vs, spot=curr.spot,
  1450	                        conv=(ga.to_commodity if ratio is not None else None),
  1451	                        etf_symbol=inst.options.symbol, buying_power=_bp,
  1452	                        net_assets=_na)
  1453	            except Exception as e:
  1454	                print(f"⚠️ {inst.key} 卖方价差失败：{type(e).__name__}: {e}", file=sys.stderr)
  1455	
  1456	            # —— 成本闸门：预期波动 vs 回本门槛（见 cost_gate 模块注释）——
  1457	            cost_html = ""
  1458	            try:
  1459	                if _ti and _ti.get("side") in ("看涨", "看跌"):
  1460	                    _cands = cost_candidates(curr, curr.spot, _ti["side"], obs_day,
  1461	                                             decidable=_ti["decidable"])
  1462	                    cost_html = render_cost_gate(
  1463	                        _cands, spot=curr.spot, side=_ti["side"],
  1464	                        conv=(ga.to_commodity if ratio is not None else None),
  1465	                        etf_symbol=inst.options.symbol)
  1466	            except Exception as e:
  1467	                print(f"⚠️ {inst.key} 成本闸门失败：{type(e).__name__}: {e}", file=sys.stderr)
  1468	
  1469	            # —— 期权结构按到期分层（近端置顶）——
  1470	            # 主报告的墙来自 analyze_gamma 的跨到期加总，会造出实盘不存在的位置；
   830	                f"{mod} 不得引用 backmonth —— 远月只作长期背景"
   831	
   832	    src = (Path(__file__).resolve().parents[1] / "undertow" / "report"
   833	           / "html.py").read_text("utf-8")
   834	    card = src[src.index("def render_backmonth"):]
   835	    card = card[:card.index("\n\ndef ") if "\n\ndef " in card else len(card)]
   836	    assert "与本周方向无关" in card, "卡片必须自带时间尺度标注"
   837	    assert "不进综合分" in card and "不改任何近月位点" in card
   838	
   839	    assert bm.BM_MIN_DTE >= 46, "远月起点必须与 gamma 的 far 层对齐"
   840	    assert bm.BM_MIN_GROWTH > 0 and bm.BM_MIN_DOI >= 1000, "双门槛缺一不可"
   841	
   842	
   843	def test_ratio_spread_needs_two_dimensions():
   844	    """比例价差必须靠 ΔOI + 成交量双维确认，否则全是随机凑对。
   845	
   846	    2026-08-31 零假设检验（随机打乱 ΔOI 后重跑）：
   847	      纯 ΔOI 口径 真实/随机 = GLD 1.5x、SLV 1.3x、QQQ 1.3x、TQQQ 1.0x
   848	      加成交量确认后           = GLD 2.9x、SLV 3.8x、QQQ 2.9x、TQQQ 6.2x
   849	    TQQQ 的 1.0x 意味着纯 ΔOI 检出的全部是巧合。
   850	    """
   851	    from undertow.analyze.flow import (detect_ratio_spreads, RATIO_VOL_TOL,
   852	                                       RATIO_MIN_VOL, TAIL_MAX_DELTA)
   853	    from undertow.analyze.validation import REGISTRY
   854	    src = (Path(__file__).resolve().parents[1] / "undertow" / "analyze"
   855	           / "flow.py").read_text("utf-8")
   856	    fn = src[src.index("def detect_ratio_spreads"):]
   857	    assert "curr_volume" in fn, "必须用成交量做第二维确认"
   858	    assert RATIO_VOL_TOL > 0 and RATIO_MIN_VOL > 0
   859	    # 零假设检验的结论必须留在代码里
   860	    assert "零假设" in src and "随机打乱" in src
   861	
   862	    v = REGISTRY["ratio_spread"]
   863	    assert "未" in v.caveat and "交易价值" in v.caveat, \
   864	        "必须写明只验证了非随机，未验证交易价值"
   865	
   866	    card = (Path(__file__).resolve().parents[1] / "undertow" / "report"
   867	            / "html.py").read_text("utf-8")
   868	    card = card[card.index("def render_ratio_spreads"):]
   869	    assert "零假设检验" in card and "残余噪音" in card
   870	    assert TAIL_MAX_DELTA <= 0.15
   871	
   872	
   873	def test_credit_wall_never_sells_itm():
   874	    """墙位卖方价差绝不能卖出【实值】腿 —— 那不是收权利金，是直接接货。
   875	
   876	    2026-08-31 实测的真实 bug：GLD 现价 407.23、put 墙 405，
   877	    激进档「墙内 2%」把目标推到 405×1.02=413.1，越过现价 6 美元，
   878	    模块给出「卖 413P」——一张深度实值 put。
   879	    卖腿必须虚值，且距现价至少 0.5%。
   880	    """
   881	    from datetime import date as _d
   882	    from undertow.analyze.credit_wall import (propose, RISK_TIERS, tier_params,
   883	                                              OFFSET_TRADEOFF, MIN_RATIO)
   884	    src = (Path(__file__).resolve().parents[1] / "undertow" / "analyze"
   885	           / "credit_wall.py").read_text("utf-8")
   886	    fn = src[src.index("def propose"):]
   887	    assert "spot * 1.001" in fn and "spot * 0.999" in fn, "必须把卖腿钳制在虚值一侧"
   888	    assert "无虚值腿可卖" in fn and "距现价不足 0.5%" in fn, "两道保护都要出声"
   889	
   890	    # 权利金与风险是同一枚硬币 —— 这张对照表是「不能一味卖近」的凭证
   891	    for i in range(len(OFFSET_TRADEOFF) - 1):
   892	        off_a, cw_a, br_a, *_ = OFFSET_TRADEOFF[i]
   893	        off_b, cw_b, br_b, *_ = OFFSET_TRADEOFF[i + 1]
   894	        assert off_a < off_b
   895	        assert cw_a >= cw_b, "卖得越近，credit/width 必须越高"
   896	        assert br_a >= br_b, "卖得越近，破墙率必须越高"
   897	
   898	    # 三档必须各自带回测证据，且激进档要标出爆仓风险
   899	    for k, t in RISK_TIERS.items():
   900	        for f in ("n", "win_rate", "break_rate", "per_trade_pct",
   901	                  "annual_pct", "median_occupancy", "worst_pct"):
   902	            assert f in t, f"{k} 缺 {f}"
   903	    assert RISK_TIERS["aggressive"]["win_rate"] < RISK_TIERS["conservative"]["win_rate"]
   904	    assert RISK_TIERS["aggressive"]["break_rate"] > RISK_TIERS["conservative"]["break_rate"]
   905	    assert "爆仓" in RISK_TIERS["aggressive"]["note"]
   906	    # 提前平仓的负面结论必须留在代码里，防止日后又"优化"成收50%就跑
   907	    assert "提前平仓实测【更差】" in src
   908	
   909	    class _C:
   910	        def __init__(s, k, st_, e, oi, bid=1.0, ask=1.1):
   911	            s.kind, s.strike, s.expiry, s.open_interest = k, st_, e, oi
   912	            s.bid, s.ask = bid, ask
   913	
   914	    exp = _d(2026, 9, 18)
   915	    obs = _d(2026, 8, 28)
   916	    cs = [_C("P", 405.0, exp, 50000), _C("P", 397.0, exp, 3000),
   917	          _C("P", 389.0, exp, 2000), _C("P", 413.0, exp, 100)]
   918	
   919	    class _S:
   920	        contracts = cs
   921	    v = propose(_S(), obs, 407.23, "看涨", 6.0, tier="aggressive")
   922	    for sp in v.spreads:
   923	        assert sp.sell_strike < 407.23, f"卖腿 {sp.sell_strike} 是实值，绝不允许"
   924	
   925	    # 中等信号必须被拒
   926	    assert not propose(_S(), obs, 407.23, "看涨", 3.0).ok
   927	    assert tier_params("conservative")["break_rate"] < 0.2
   928	    print("PASS test_credit_wall_never_sells_itm")
   929	
   930	
   931	def test_sizing_is_kelly_not_fixed_pct():
   932	    """仓位按 Kelly 判，不按净资产固定百分比。
   933	
   934	    用户 2026-08-31：「原本我们的守则是仓位管理风险 10%？但是为了这个 10%，
   935	    却可能放弃更优的交易，而选择次优，这反而放大了风险。风险管理得再好，
   936	    永远在亏损有啥用呢。」
   937	
   938	    这不是偏好问题是算术：净资产 $264 × 10% = $26，买不起任何一张价差
   939	    （最小占用 $86）。固定百分比对小账户 = 强制选择负期望的廉价合约。
   940	
   941	    锁三件事：
   942	      ① 负优势时必须拒绝（仓位再小也是慢慢亏）
   943	      ② 1 组超过 Kelly 时不得静默压到 0 —— 那等于"不能交易"，
   944	         要把超配倍数摆出来交给人决定
   945	      ③ 盈亏比（b）必须真的按 赢/亏 算，且实测里激进档的 b 高于稳健档
   946	    """
   947	    from undertow.analyze.sizing import kelly, size, ruin_probability
   948	    from undertow.analyze.credit_wall import RISK_TIERS
   949	
   950	    # ③ 关键反直觉事实：激进档胜率低但盈亏比远高（买腿保护近，破墙也只破一点）
   951	    cons = RISK_TIERS["conservative"]
   952	    aggr = RISK_TIERS["aggressive"]
   953	    kc = kelly(cons["win_rate"], cons["per_trade_pct"], cons["win_roi_pct"])
   954	    ka = kelly(aggr["win_rate"], aggr["per_trade_pct"], aggr["win_roi_pct"])
   955	    assert aggr["win_rate"] < cons["win_rate"], "激进档胜率更低"
   956	    assert ka.odds > kc.odds * 3, "但激进档盈亏比必须显著更高，这是它能上仓位的原因"
   957	    assert ka.kelly > kc.kelly, "Kelly 应据此给激进档更大仓位"
   958	    assert kc.lose_roi > ka.lose_roi, "稳健档输的时候亏得更多（卖得远=被打穿即深实值）"
   959	
   960	    # ① 负优势拒绝
   961	    bad = kelly(0.30, -5.0, 10.0)
   962	    assert not bad.positive_edge
   963	    v = size(264.0, 100.0, bad)
   964	    assert not v.ok and "不该做" in v.reason
   965	
   966	    # ② 1 组超 Kelly 不得静默归零
   967	    v2 = size(264.0, 96.0, kc, buying_power=153.0)
   968	    assert v2.ok and v2.n_units == 1, "不得因超 Kelly 就判成不能做"
   969	    assert v2.over_kelly > 1.0
   970	    assert "不存在" in v2.reason, "必须说明「压到 Kelly 以下的选项不存在」"
   971	
   972	    # 买不起要明说
   973	    v3 = size(264.0, 900.0, ka, buying_power=153.0)
   974	    assert not v3.ok and "做不了" in v3.reason
   975	
   976	    # 破产概率随胜率下降而升高
   977	    assert ruin_probability(0.23, 3) > ruin_probability(0.82, 3) * 10
   978	
   979	    src = (Path(__file__).resolve().parents[1] / "undertow" / "analyze"
   980	           / "sizing.py").read_text("utf-8")
   981	    assert "10%" in src and "买不起" in src, "固定百分比失效的算术必须留在代码里"
   982	    # 术语与 risk_reward 保持一致
   983	    assert "盈亏比" in src and "赔率" not in src
   984	    print("PASS test_sizing_is_kelly_not_fixed_pct")
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-8cOjhGdC' (errno=Operation not permitted)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-30HJzyPb' (errno=Operation not permitted)
commit bb22ef6c46c2de51bfc6c610fa7d4c0ed7ff0147
Author:     YuhaoDoong <yuhaodong999@gmail.com>
AuthorDate: Tue Sep 1 12:51:19 2026 +0800
Commit:     YuhaoDoong <yuhaodong999@gmail.com>
CommitDate: Tue Sep 1 12:51:19 2026 +0800

    feat: 墙位卖方价差模块 —— 三个风险档，卖腿位置/宽度/到期全网格实测
    
    用户 2026-08-31：「我们目前最可靠的期权结构/墙等数据，最适合的交易策略应该是
    卖方价差。因为事实证明破墙很难。」
    
    ## 回测（136 份快照、45 个极强信号、逐日真实盘口重估）
    
    卖腿位置决定一切 —— 权利金和风险是同一枚硬币（宽5%、15~45天、持有到期）：
    
      卖腿位置   credit/width  破墙率  胜率   单笔    年化
      墙内4%        28%        30%   77%  +3.20%  +44%
      墙内2%        21%        21%   81%  +0.61%   +8%
      卖墙上        16%        20%   89%  +2.87%  +40%
      墙外2%        12%         9%   84%  +3.62%  +50%
      墙外4%         9%         5%   86%  +3.68%  +50%
    
    用户当初那笔 SLV 61P/60P（墙在 60、卖 61 = 墙内 1.7%、6 天到期）正落在
    数据上年化最高的区域 —— 他的直觉是对的，我此前只测了「墙外 2%」这一个
    保守配置就下结论，是我把问题问窄了。
    
    ## 提前平仓实测【更差】，与通行说法相反
    
    逐日用真实 bid/ask 重估（不是 BS 插值）：
      卖墙上：持有到期 +40% ／ 赚50%平 +5% ／ 赚70%平 +48%
      墙外2%：持有到期 +50% ／ 赚50%平 +13%
    破墙率只有 5~35%，而平仓要双向吃点差，不如让它到期作废。
    该结论写进模块注释并由测试锁住，防止日后又「优化」成收50%就跑。
    
    ## 三个风险档
    
      稳健  墙外2%·宽2.0%·15~45天  38笔 胜率82% 破墙11% 单笔+2.84% 年化+37%  占用$96
      平衡  卖墙上·宽2.5%·15~45天  45笔 胜率76% 破墙20% 单笔-0.43% 年化 -6%  占用$117
      激进  墙内2%·宽2.5%· 4~14天  60笔 胜率63% 破墙35% 单笔+9.99% 年化+413% 占用$118
    
    激进档年化最高，但胜率仅 63%，连亏 3 次概率 5.1%、损失 $354 —— 对当前
    $264 的账户是爆仓级风险，note 字段直书此事，测试断言「爆仓」二字必须在。
    
    ## 修掉一个会直接害人的 bug
    
    墙内偏移可能越过现价：GLD 现价 407.23、put 墙 405，墙内2% 推到 413.1，
    模块给出「卖 413P」——一张深度实值 put，那不是收权利金是直接接货。
    现在卖腿被钳制在虚值一侧，且距现价至少 0.5%，两道保护都会出声说明。
    
    ## 顺带更正三处此前的错误结论
    
    - 「越快到期越不划算」：只看单笔收益率成立，看年化则相反（短到期赢在周转，
      4~14天 墙内2% 年化 +413% vs 15~45天 +8%）。用户的直觉对。
    - 「同到期专属墙更准」：实测相反，同到期口径无论门槛多高都是负的
      （-5%~-17%），近端加总墙随门槛单调改善。跨到期共同堆积才是真关键位。
    - 铁鹰：破墙率 57~80%（双边都要守住），全部负收益，已否决。
    
    ⚠️ 所有阈值在同一批数据上选出，多重比较风险实打实；年化是
       「单笔 × 365/持有天数」的外推，未扣信号空窗期。
    
    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
    Claude-Session: https://claude.ai/code/session_015nR6EdboHusPmkZFVGNhwZ

 data/logs/lb_timing_2026-08-31.log |   5 +
 data/logs/lb_timing_2026-09-01.log |   3 +
 tests/test_position_alert.py       |  58 +++++++
 undertow/analyze/credit_wall.py    | 300 +++++++++++++++++++++++++++++++++++++
 4 files changed, 366 insertions(+)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-pwalBX2I' (errno=Operation not permitted)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-JpSUJ6uE' (errno=Operation not permitted)
commit bb22ef6c46c2de51bfc6c610fa7d4c0ed7ff0147
Author: YuhaoDoong <yuhaodong999@gmail.com>
Date:   Tue Sep 1 12:51:19 2026 +0800

    feat: 墙位卖方价差模块 —— 三个风险档，卖腿位置/宽度/到期全网格实测
    
    用户 2026-08-31：「我们目前最可靠的期权结构/墙等数据，最适合的交易策略应该是
    卖方价差。因为事实证明破墙很难。」
    
    ## 回测（136 份快照、45 个极强信号、逐日真实盘口重估）
    
    卖腿位置决定一切 —— 权利金和风险是同一枚硬币（宽5%、15~45天、持有到期）：
    
      卖腿位置   credit/width  破墙率  胜率   单笔    年化
      墙内4%        28%        30%   77%  +3.20%  +44%
      墙内2%        21%        21%   81%  +0.61%   +8%
      卖墙上        16%        20%   89%  +2.87%  +40%
      墙外2%        12%         9%   84%  +3.62%  +50%
      墙外4%         9%         5%   86%  +3.68%  +50%
    
    用户当初那笔 SLV 61P/60P（墙在 60、卖 61 = 墙内 1.7%、6 天到期）正落在
    数据上年化最高的区域 —— 他的直觉是对的，我此前只测了「墙外 2%」这一个
    保守配置就下结论，是我把问题问窄了。
    
    ## 提前平仓实测【更差】，与通行说法相反
    
    逐日用真实 bid/ask 重估（不是 BS 插值）：
      卖墙上：持有到期 +40% ／ 赚50%平 +5% ／ 赚70%平 +48%
      墙外2%：持有到期 +50% ／ 赚50%平 +13%
    破墙率只有 5~35%，而平仓要双向吃点差，不如让它到期作废。
    该结论写进模块注释并由测试锁住，防止日后又「优化」成收50%就跑。
    
    ## 三个风险档
    
      稳健  墙外2%·宽2.0%·15~45天  38笔 胜率82% 破墙11% 单笔+2.84% 年化+37%  占用$96
      平衡  卖墙上·宽2.5%·15~45天  45笔 胜率76% 破墙20% 单笔-0.43% 年化 -6%  占用$117
      激进  墙内2%·宽2.5%· 4~14天  60笔 胜率63% 破墙35% 单笔+9.99% 年化+413% 占用$118
    
    激进档年化最高，但胜率仅 63%，连亏 3 次概率 5.1%、损失 $354 —— 对当前
    $264 的账户是爆仓级风险，note 字段直书此事，测试断言「爆仓」二字必须在。
    
    ## 修掉一个会直接害人的 bug
    
    墙内偏移可能越过现价：GLD 现价 407.23、put 墙 405，墙内2% 推到 413.1，
    模块给出「卖 413P」——一张深度实值 put，那不是收权利金是直接接货。
    现在卖腿被钳制在虚值一侧，且距现价至少 0.5%，两道保护都会出声说明。
    
    ## 顺带更正三处此前的错误结论
    
    - 「越快到期越不划算」：只看单笔收益率成立，看年化则相反（短到期赢在周转，
      4~14天 墙内2% 年化 +413% vs 15~45天 +8%）。用户的直觉对。
    - 「同到期专属墙更准」：实测相反，同到期口径无论门槛多高都是负的
      （-5%~-17%），近端加总墙随门槛单调改善。跨到期共同堆积才是真关键位。
    - 铁鹰：破墙率 57~80%（双边都要守住），全部负收益，已否决。
    
    ⚠️ 所有阈值在同一批数据上选出，多重比较风险实打实；年化是
       「单笔 × 365/持有天数」的外推，未扣信号空窗期。
    
    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
    Claude-Session: https://claude.ai/code/session_015nR6EdboHusPmkZFVGNhwZ

diff --git a/undertow/analyze/credit_wall.py b/undertow/analyze/credit_wall.py
new file mode 100644
index 0000000..2935a0c
--- /dev/null
+++ b/undertow/analyze/credit_wall.py
@@ -0,0 +1,300 @@
+"""墙位卖方价差 —— 把「墙难破」这件事直接做成仓位。
+
+用户 2026-08-31 提出：「我们目前最可靠的期权结构/墙等数据，最适合的交易策略
+应该是卖方价差。因为事实证明破墙很难。」回测证实了这个直觉，但需要三道闸门，
+少一道就变成亏损策略。
+
+═══ 回测（2026-08-31，样本区间 2026-06-25~08-31，7 个品种）═══
+
+【为什么必须用「近端加总墙」而不是「同到期专属墙」】
+用户当时追问「墙的日期也要考虑」——我第一版正是用近端(≤14天)加总的墙去卖
+22~45 天的价差，口径错配。但改成同到期专属墙后结果反而崩了：
+
+  近端加总墙 · 厚度≥10%   45笔  84%胜率  +3.62%/笔  年化 +50%
+  近端加总墙 · 厚度≥15%   14笔  86%胜率  +9.73%/笔  年化 +133%
+  近端加总墙 · 厚度≥20%    9笔 100%胜率 +11.56%/笔  年化 +147%
+  同到期专属墙 · ≥25%     55笔  76%胜率  -5.26%/笔  年化 -72%
+  同到期专属墙 · ≥45%     22笔  68%胜率 -16.55%/笔  年化 -208%
+
+同到期口径提高门槛不但没改善、反而更差，所以这不是幸存者偏差：
+**跨到期都堆在同一位置＝真关键位；单个到期的最大 OI＝噪音。**
+正确用法是把同到期当【第二重确认】——加总墙的位置上，该到期自己也要有墙：
+
+  加总≥10% + 同到期≥20% · 15~45天   13笔 92%胜率 +8.93%/笔 年化+98% p=0.003
+  加总≥10% + 同到期≥20% ·  4~14天   23笔 78%胜率 +1.15%/笔 年化+47% p=0.011
+
+【为什么必须是极强信号】亏损全集中在中等信号：
+  极强(≥5×)  亏损笔最惨 -24
+  中等(2~5×) 亏损笔 -1250 / -499 / -260 —— 大亏 50 倍
+【为什么必须看墙的厚度】亏损笔的墙平均 29,969、盈利笔 65,789（差 2.2 倍）；
+  墙 OI <30,000 时破墙率 55%，≥60,000 时降到 21%。
+  绝对 OI 不可跨品种比较（QQQ 的 4 万 ≠ SLV 的 4 万），故一律用相对占比。
+
+【到期时间】用户猜「越快到期越不容易破墙」——实测不成立，破墙率与 DTE 无关
+（20~29% 横跨 1~45 天）。但胜率随 DTE 单调上升（56%→86%），因为收到的
+权利金更厚、缓冲更大。15~45 天单笔收益率最高。
+
+【铁鹰不成立】双边同时卖，破墙率飙到 57~80%（任一边破就亏）：
+  全信号 4~14天 50%胜率 -3.06%/笔　全信号 15~45天 64%胜率 -4.70%/笔
+  极强 15~45天 71%胜率 -3.14%/笔 —— 全负。已否决，勿再试。
+
+⚠️ 所有阈值都是在同一批数据上选出来的，多重比较风险实打实；
+   最优组合仅 13 笔。这套参数需要样本外验证才算数。
+"""
+from __future__ import annotations
+
+from dataclasses import dataclass
+from datetime import date
+
+MIN_RATIO = 5.0          # 只在极强信号做（中等信号的亏损是极强的 50 倍）
+MIN_WALL_SHARE = 0.10    # 加总墙厚度：占该侧近端总 OI 的比例
+MIN_EXP_SHARE = 0.20     # 第二重确认：该到期自己的墙也要够厚
+WALL_MATCH_TOL = 0.02    # 同到期墙必须落在加总墙 ±2% 内才算「同一位置」
+FEE_PER_LEG = 0.80
+
+# ═══ 三个风险档 ═══════════════════════════════════════════════════════════
+# 2026-08-31 完整网格（136 份快照、45 个极强信号、逐日真实盘口重估）。
+# 卖腿位置用【相对墙】的偏移：负=墙内（更靠近现价、权利金厚、破墙率高）。
+#
+# 用户当初那笔 SLV 61P/60P（8/21 开、8/26 到期）正是 aggressive 档：
+# 墙在 60、卖 61 = 墙内 1.7%、6 天到期 —— 落在数据上年化最高的区域。
+#
+# ⚠️ 提前平仓实测【更差】：破墙率只有 5~35%，而平仓要双向吃点差，
+#    不如让它到期作废。赚50%平 vs 持有到期：卖墙上 +5% vs +40% 年化。
+#    这与"卖方应该收50%就跑"的通行说法相反，但数据如此。
+RISK_TIERS = {
+    "conservative": {
+        "label": "稳健", "offset": 0.02, "width": 0.020, "dte": (15, 45),
+        "n": 38, "win_rate": 0.82, "break_rate": 0.11, "per_trade_pct": 2.84,
+        "annual_pct": 37, "median_occupancy": 96, "worst_pct": -103,
+        "note": "破墙率 11% 最低。账户小的时候先活下来。",
+    },
+    "balanced": {
+        "label": "平衡", "offset": 0.0, "width": 0.025, "dte": (15, 45),
+        "n": 45, "win_rate": 0.76, "break_rate": 0.20, "per_trade_pct": -0.43,
+        "annual_pct": -6, "median_occupancy": 117, "worst_pct": -103,
+        "note": "卖在墙上。窄宽度下单笔为负——权利金没覆盖住破墙损失。",
+    },
+    "aggressive": {
+        "label": "激进", "offset": -0.02, "width": 0.025, "dte": (4, 14),
+        "n": 60, "win_rate": 0.63, "break_rate": 0.35, "per_trade_pct": 9.99,
+        "annual_pct": 413, "median_occupancy": 118, "worst_pct": -105,
+        "note": "年化最高，但胜率仅 63%、破墙 35%。连亏 3 次概率 5.1%，"
+                "对小账户是爆仓级风险。",
+    },
+}
+DEFAULT_TIER = "conservative"
+
+# 兼容旧调用
+SELL_OFFSET = RISK_TIERS[DEFAULT_TIER]["offset"]
+WIDTH_FRAC = RISK_TIERS[DEFAULT_TIER]["width"]
+DTE_MIN, DTE_MAX = RISK_TIERS[DEFAULT_TIER]["dte"]
+
+BACKTEST = {
+    "n": 38, "win_rate": 0.82, "per_trade_pct": 2.84, "annual_pct": 37,
+    "p_value": 0.000, "median_occupancy": 96,
+    "caveat": "38 笔，阈值在同一批数据上选出，需样本外验证；"
+              "年化是「单笔 × 365/持有天数」的外推，未扣信号空窗",
+}
+
+# 卖腿位置 → 权利金比例与破墙率的完整对照（宽5%、15~45天、持有到期）
+# 这张表是「为什么不能一味卖近」的凭证：权利金和风险是同一枚硬币。
+OFFSET_TRADEOFF = [
+    # (偏移, credit/width, 破墙率, 胜率, 单笔%, 年化%)
+    (-0.04, 0.28, 0.30, 0.77, 3.20, 44),
+    (-0.02, 0.21, 0.21, 0.81, 0.61, 8),
+    (0.00, 0.16, 0.20, 0.89, 2.87, 40),
+    (0.02, 0.12, 0.09, 0.84, 3.62, 50),
+    (0.04, 0.09, 0.05, 0.86, 3.68, 50),
+]
+
+
+@dataclass(frozen=True)
+class WallSpread:
+    kind: str                # 'C' 卖看涨价差 / 'P' 卖看跌价差
+    expiry: date
+    dte: int
+    sell_strike: float
+    buy_strike: float
+    credit: float            # 每张净收权利金($)，卖腿吃 bid、买腿吃 ask
+    width: float
+    occupancy: float         # 保证金占用 = (宽度 - 权利金)×100
+    wall_strike: float
+    wall_share: float        # 加总墙厚度占比
+    exp_share: float         # 该到期自己的墙厚度占比
+    buffer_pct: float        # 卖腿距现价的缓冲
+
+    @property
+    def max_loss(self) -> float:
+        return self.occupancy + FEE_PER_LEG * 4
+
+    @property
+    def roi(self) -> float:
+        return self.credit / self.occupancy if self.occupancy > 0 else 0.0
+
+    @property
+    def annual_roi(self) -> float:
+        return self.roi * 365 / max(self.dte, 1)
+
+
+@dataclass(frozen=True)
+class Verdict:
+    ok: bool
+    reason: str
+    spreads: list[WallSpread]
+    gates: dict
+
+
+def _walls_aggregate(snap, obs: date, kind: str, spot: float, max_dte: int = 14):
+    """近端加总墙 + 其占该侧总 OI 的比例。"""
+    agg: dict[float, int] = {}
+    total = 0
+    for c in snap.contracts:
+        if c.kind != kind:
+            continue
+        d = (c.expiry - obs).days
+        if not (1 <= d <= max_dte):
+            continue
+        if kind == "C" and c.strike < spot:
+            continue
+        if kind == "P" and c.strike > spot:
+            continue
+        if not (spot * 0.85 <= c.strike <= spot * 1.15):
+            continue
+        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
+        total += c.open_interest
+    if not agg or total <= 0:
+        return None, 0, 0.0
+    k, v = max(agg.items(), key=lambda x: x[1])
+    return k, v, v / total
+
+
+def _wall_same_expiry(snap, exp: date, kind: str, spot: float):
+    agg: dict[float, int] = {}
+    total = 0
+    for c in snap.contracts:
+        if c.expiry != exp or c.kind != kind:
+            continue
+        if kind == "C" and c.strike < spot:
+            continue
+        if kind == "P" and c.strike > spot:
+            continue
+        if not (spot * 0.85 <= c.strike <= spot * 1.15):
+            continue
+        agg[c.strike] = agg.get(c.strike, 0) + c.open_interest
+        total += c.open_interest
+    if not agg or total <= 0:
+        return None, 0, 0.0
+    k, v = max(agg.items(), key=lambda x: x[1])
+    return k, v, v / total
+
+
+def tier_params(tier: str = DEFAULT_TIER) -> dict:
+    """取风险档参数。未知档位直接抛，不静默回落 —— 档位决定的是爆仓风险。"""
+    if tier not in RISK_TIERS:
+        raise ValueError(f"未知风险档 {tier!r}，可选：{list(RISK_TIERS)}")
+    return RISK_TIERS[tier]
+
+
+def propose(snap, obs: date, spot: float, direction: str, ratio: float,
+            tier: str = DEFAULT_TIER) -> Verdict:
+    """给出墙位卖方价差候选。三道闸门任一不过就不出候选，并说明卡在哪。
+
+    direction: '看涨'/'看跌' —— 卖【逆向】侧：看涨卖 put 价差、看跌卖 call 价差。
+    """
+    gates = {"ratio": ratio, "min_ratio": MIN_RATIO}
+    if ratio < MIN_RATIO:
+        return Verdict(False, (f"压力倍数 {ratio:.1f}× 未达 {MIN_RATIO:g}× —— "
+                               f"回测里中等信号(2~5×)的亏损笔达 -1250/-499/-260，"
+                               f"是极强信号(最惨 -24)的 50 倍。这道闸门不能松。"),
+                       [], gates)
+    if direction not in ("看涨", "看跌"):
+        return Verdict(False, f"方向不明（{direction}），无从决定卖哪一侧", [], gates)
+
+    kind = "P" if direction == "看涨" else "C"
+    gates["side"] = kind
+    wk, woi, wshare = _walls_aggregate(snap, obs, kind, spot)
+    gates.update({"wall_strike": wk, "wall_oi": woi, "wall_share": wshare,
+                  "min_wall_share": MIN_WALL_SHARE})
+    if wk is None or wshare < MIN_WALL_SHARE:
+        return Verdict(False, (f"{kind} 侧近端加总墙厚度 "
+                               f"{wshare:.0%} < {MIN_WALL_SHARE:.0%} —— "
+                               f"回测里薄墙(OI<30,000)的破墙率 55%，厚墙(≥60,000)只有 21%；"
+                               f"亏损笔的墙平均只有盈利笔的一半。"),
+                       [], gates)
+
+    tp = tier_params(tier)
+    dte_lo, dte_hi = tp["dte"]
+    gates["tier"] = tier
+    gates["tier_label"] = tp["label"]
+    legs_by_exp: dict[date, list] = {}
+    for c in snap.contracts:
+        if c.kind != kind or not (c.bid and c.ask and c.bid > 0):
+            continue
+        d = (c.expiry - obs).days
+        if dte_lo <= d <= dte_hi:
+            legs_by_exp.setdefault(c.expiry, []).append(c)
+
+    out: list[WallSpread] = []
+    skipped: list[str] = []
+    for exp in sorted(legs_by_exp):
+        ewk, ewoi, eshare = _wall_same_expiry(snap, exp, kind, spot)
+        # 第二重确认：该到期自己的墙必须落在加总墙同一位置且够厚
+        if ewk is None or abs(ewk - wk) / wk > WALL_MATCH_TOL:
+            skipped.append(f"{exp} 该到期的墙在 "
+                           f"{ewk:g}（与加总墙 {wk:g} 不同位）" if ewk else f"{exp} 无墙")
+            continue
+        if eshare < MIN_EXP_SHARE:
+            skipped.append(f"{exp} 同到期墙厚度 {eshare:.0%} < {MIN_EXP_SHARE:.0%}")
+            continue
+        ls = sorted(legs_by_exp[exp], key=lambda c: c.strike)
+        off = tp["offset"]
+        tgt = wk * (1 + off) if kind == "C" else wk * (1 - off)
+        # ⚠️ 墙内偏移不得越过现价：墙本身可能已经很贴近现价，再往内推就成了
+        # 卖【实值】腿 —— 那不是收权利金，是直接接货。2026-08-31 实测：
+        # GLD 现价 407.23、put 墙 405，墙内 2% 推到 413.1，越过现价 6 美元。
+        if kind == "C":
+            tgt = max(tgt, spot * 1.001)
+            pool = [c for c in ls if c.strike > spot]
+        else:
+            tgt = min(tgt, spot * 0.999)
+            pool = [c for c in ls if c.strike < spot]
+        if not pool:
+            skipped.append(f"{exp} 无虚值腿可卖")
+            continue
+        sell = min(pool, key=lambda c: abs(c.strike - tgt))
+        wf = tp["width"]
+        wt = sell.strike * (1 + wf) if kind == "C" else sell.strike * (1 - wf)
+        cands = [c for c in ls if (c.strike > sell.strike if kind == "C"
+                                   else c.strike < sell.strike)]
+        # 缓冲太薄的直接弃 —— 卖腿贴着现价时权利金再厚也扛不住一天的波动
+        if abs(sell.strike / spot - 1) < 0.005:
+            skipped.append(f"{exp} 卖腿 {sell.strike:g} 距现价不足 0.5%")
+            continue
+        if not cands:
+            continue
+        buy = min(cands, key=lambda c: abs(c.strike - wt))
+        credit = (sell.bid - buy.ask) * 100
+        width = abs(buy.strike - sell.strike) * 100
+        if credit <= 0 or width <= 0:
+            continue
+        out.append(WallSpread(
+            kind=kind, expiry=exp, dte=(exp - obs).days,
+            sell_strike=sell.strike, buy_strike=buy.strike,
+            credit=credit, width=width, occupancy=width - credit,
+            wall_strike=wk, wall_share=wshare, exp_share=eshare,
+            buffer_pct=abs(sell.strike / spot - 1) * 100))
+    gates["skipped"] = skipped[:5]
+    if not out:
+        return Verdict(False, (f"加总墙在 {wk:g}（厚度 {wshare:.0%}，已达标），"
+                               f"但 {dte_lo}~{dte_hi} 天内没有到期能通过第二重确认"
+                               f"（该到期自己的墙需落在同一位置且占比 ≥{MIN_EXP_SHARE:.0%}）。"
+                               f"　当前档位 {tp['label']}：{dte_lo}~{dte_hi} 天、"
+                               f"卖腿{'墙内' if tp['offset'] < 0 else '墙外'}"
+                               f"{abs(tp['offset']) * 100:.0f}%、宽{tp['width'] * 100:.1f}%。"
+                               + ("　跳过原因：" + "；".join(skipped[:3]) if skipped else "")),
+                       [], gates)
+    out.sort(key=lambda s: -s.annual_roi)
+    return Verdict(True, (f"压力比 {ratio:.1f}× 过闸，{kind} 侧加总墙 {wk:g} "
+                          f"厚度 {wshare:.0%}，{len(out)} 个到期通过同到期确认。"),
+                   out, gates)

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "nl -ba undertow/cli.py | sed -n '1434,1465p'; rg -n \"obs_day\\s*=|today\\s*=\" undertow/cli.py | head -30; nl -ba undertow/analyze/gamma.py | sed -n '440,510p'; rg -n '\"captured_at\"|\"as_of\"|\"spot\"' data/snapshots/options/GLD/2026-08-31.json.gz undertow/collect -g '*.py'" in /Users/yhdong/Trading
 succeeded in 0ms:
  1434	            # —— 墙位卖方价差候选（analyze/credit_wall）——
  1435	            credit_wall_html = ""
  1436	            try:
  1437	                if _ti and _ti.get("side") in ("看涨", "看跌"):
  1438	                    _bp = _na = None
  1439	                    try:
  1440	                        from undertow.collect.longbridge_account import fetch_assets
  1441	                        _a = fetch_assets()
  1442	                        _bp, _na = _a.buy_power, _a.net_assets
  1443	                    except Exception:
  1444	                        pass
  1445	                    _vs = {t: cw_propose(curr, obs_day, curr.spot, _ti["side"],
  1446	                                         _ti["ratio"], tier=t)
  1447	                           for t in ("conservative", "balanced", "aggressive")}
  1448	                    credit_wall_html = render_credit_wall(
  1449	                        _vs, spot=curr.spot,
  1450	                        conv=(ga.to_commodity if ratio is not None else None),
  1451	                        etf_symbol=inst.options.symbol, buying_power=_bp,
  1452	                        net_assets=_na)
  1453	            except Exception as e:
  1454	                print(f"⚠️ {inst.key} 卖方价差失败：{type(e).__name__}: {e}", file=sys.stderr)
  1455	
  1456	            # —— 成本闸门：预期波动 vs 回本门槛（见 cost_gate 模块注释）——
  1457	            cost_html = ""
  1458	            try:
  1459	                if _ti and _ti.get("side") in ("看涨", "看跌"):
  1460	                    _cands = cost_candidates(curr, curr.spot, _ti["side"], obs_day,
  1461	                                             decidable=_ti["decidable"])
  1462	                    cost_html = render_cost_gate(
  1463	                        _cands, spot=curr.spot, side=_ti["side"],
  1464	                        conv=(ga.to_commodity if ratio is not None else None),
  1465	                        etf_symbol=inst.options.symbol)
120:    today = market_today()
126:    evs = upcoming(events, today=today, within_days=args.within, instrument=inst)
422:    today = market_today()
492:    today = market_today()
533:                               proxy_quality=inst.options.proxy_quality, today=today,
535:            fa = analyze_flow(prev, curr, today=today, horizon_days=args.horizon,
561:    today = market_today()
577:            ladder = build_ladder(prev, curr, today=today,
1080:            today = date.fromisoformat(replay)
1086:        today = market_today()
1143:            obs_day = _prev_weekday(date.fromisoformat(curr_date_s)) if curr_date_s else _prev_weekday(today)
1146:                               proxy_quality=inst.options.proxy_quality, today=obs_day,
1148:            fa = analyze_flow(prev, curr, today=obs_day, horizon_days=args.horizon,
1252:            evs = upcoming(all_events, today=today, within_days=21, instrument=inst.key)
1279:                                            today=t, horizon_days=args.horizon)
1347:                                            today=prev_obs, horizon_days=args.horizon)
1398:            condor_plan = assess_condor(snap=curr, vr=vr, today=today, fa=fa)
1399:            cs_plan = assess_credit_spread(snap=curr, vr=vr, outlook=outlook, today=today, fa=fa)
1494:                ladder = build_ladder(prev, curr, today=today, multiplier=mult,
1546:                        today=today.isoformat(), h4=h4_read)
1939:    obs_day = _prev_weekday(date.fromisoformat(curr_date_s)) if curr_date_s else _prev_weekday(today)
1942:                       today=obs_day, horizon_days=45)
1943:    fa = analyze_flow(prev, curr, today=obs_day, horizon_days=45,
2057:    today = market_today()
2091:    contexts, today = _build_contexts(positions, no_cache, live_quotes=live_quotes)
2126:    today = bundle["today"]
2318:    today = market_today()
2535:                    fa = analyze_flow(snaps[pv], snaps[cu], today=cu,
2684:    today = market_today()
2715:    today = bundle["today"]
   440	#   照混算读会以为下方有 8.9% 缓冲，实际支撑就在脚下。
   441	#
   442	# 分层后还能读出一个混算永远给不出的信息：**跨层一致性**。
   443	# 同日 GLD call 墙三层都是 430 → 真共识位；put 墙三层各不相同 → 加总产物。
   444	# ─────────────────────────────────────────────────────────────────────────────
   445	
   446	WALL_LAYERS: tuple[tuple[str, str, int, int], ...] = (
   447	    ("near", "近端 ≤14天",   1,   14),    # 交易层：本周进出场、破位判断
   448	    ("mid",  "中端 15-45天", 15,  45),    # 布局层：多数持仓所在
   449	    ("far",  "远端 >45天",   46, 3650),   # 背景层：机构长期布局，不参与本周判断
   450	)
   451	WALL_SAME_TOL = 0.012   # 两层墙位相对差 ≤1.2% 视为同一位置（容忍行权价档距）
   452	
   453	
   454	@dataclass(frozen=True)
   455	class WallLayer:
   456	    key: str
   457	    label: str
   458	    lo_dte: int
   459	    hi_dte: int
   460	    call_wall: float
   461	    call_wall_oi: int
   462	    put_wall: float
   463	    put_wall_oi: int
   464	    call_walls_top: list[tuple[float, int]]
   465	    put_walls_top: list[tuple[float, int]]
   466	    total_call_oi: int
   467	    total_put_oi: int
   468	    n_strikes: int
   469	
   470	    @property
   471	    def empty(self) -> bool:
   472	        return self.call_wall_oi == 0 and self.put_wall_oi == 0
   473	
   474	
   475	def _layer_walls(snap, today: date, spot: float, lo: int, hi: int,
   476	                 key: str, label: str) -> WallLayer:
   477	    """单层内独立求墙——不跨层加总，这是与 analyze_gamma 的唯一实质差别。"""
   478	    by_strike: dict[float, list[int]] = {}
   479	    tc = tp = 0
   480	    wall_hi, wall_lo = spot * (1 + WALL_BAND), spot * (1 - WALL_BAND)
   481	    for c in snap.with_oi():
   482	        d = (c.expiry - today).days
   483	        if not (lo <= d <= hi):
   484	            continue
   485	        slot = by_strike.setdefault(c.strike, [0, 0])
   486	        if c.is_call:
   487	            slot[0] += c.open_interest
   488	            tc += c.open_interest
   489	        else:
   490	            slot[1] += c.open_interest
   491	            tp += c.open_interest
   492	    cs = [(s, v[0]) for s, v in by_strike.items() if spot <= s <= wall_hi and v[0] > 0]
   493	    ps = [(s, v[1]) for s, v in by_strike.items() if wall_lo <= s <= spot and v[1] > 0]
   494	    cw, cwo = max(cs, key=lambda x: x[1]) if cs else (spot, 0)
   495	    pw, pwo = max(ps, key=lambda x: x[1]) if ps else (spot, 0)
   496	    return WallLayer(
   497	        key=key, label=label, lo_dte=lo, hi_dte=hi,
   498	        call_wall=cw, call_wall_oi=cwo, put_wall=pw, put_wall_oi=pwo,
   499	        call_walls_top=sorted(cs, key=lambda x: -x[1])[:3],
   500	        put_walls_top=sorted(ps, key=lambda x: -x[1])[:3],
   501	        total_call_oi=tc, total_put_oi=tp, n_strikes=len(by_strike),
   502	    )
   503	
   504	
   505	def layered_walls(snap, today: date, spot: float) -> dict[str, WallLayer]:
   506	    return {k: _layer_walls(snap, today, spot, lo, hi, k, lab)
   507	            for k, lab, lo, hi in WALL_LAYERS}
   508	
   509	
   510	def wall_agreement(layers: dict[str, WallLayer], side: str) -> tuple[bool, str]:
undertow/collect/longbridge_options.py:123:    return {"symbol": symbol, "spot": spot, "expiries": exps,
undertow/collect/store.py:55:            "captured_at": captured_at if captured_at is not None else time.time(),

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "python3 - <<'PY'
import gzip,json
p='data/snapshots/options/GLD/2026-08-31.json.gz'
with gzip.open(p,'rt') as f:d=json.load(f)
print(d.keys()); print({k:d.get(k) for k in d if k"'!='"'contracts'}); print(d['contracts'][0])
PY
nl -ba undertow/collect/store.py | sed -n '1,100p'; nl -ba undertow/collect/cboe_options.py | sed -n '1,220p' 2>/dev/null || true; nl -ba undertow/collect/longbridge_options.py | sed -n '90,145p'" in /Users/yhdong/Trading
 succeeded in 0ms:
zsh:1: can't create temp file for here document: operation not permitted
     1	"""快照仓库：把数据源的【原始 payload 全字段】按日落盘，永久留存。
     2	
     3	为什么需要它（区别于 cache.py）:
     4	  - cache.py 是带 TTL 的临时缓存，会被新数据覆盖，目的是少打 API。
     5	  - store.py 是【永久档案】：CBOE/Yahoo 等不提供期权历史，今天的期权链
     6	    一旦不存，明天就永远拿不回来。期权 OI/IV/volume 的日级演变正是 flow 层
     7	    （近月大单异动、ΔOI/ΔIV）唯一的数据来源——必须自己每天落盘攒。
     8	  - 我们存【原始 payload】而非解析后的子集：尽可能多留字段（bid/ask/theta/vega…），
     9	    将来分析需要新字段时不必重新采集（也采集不到历史）。
    10	
    11	落盘布局（每个文件 = 某标的某日的一份完整原始 payload）:
    12	    data/snapshots/{kind}/{symbol}/{YYYY-MM-DD}.json
    13	        kind:   数据种类，目前主要是 "options"（COT/price 可从官方 API 回填，非必需）
    14	        record: {captured_at, kind, symbol, date, payload}
    15	
    16	这些文件【纳入 git】——push 到远端即等于备份这份不可再生的历史。
    17	"""
    18	from __future__ import annotations
    19	
    20	import gzip
    21	import json
    22	import os
    23	import sys
    24	import time
    25	from datetime import date
    26	from pathlib import Path
    27	from typing import Any
    28	
    29	from undertow.core.config import DATA_DIR
    30	
    31	SNAPSHOT_DIR = DATA_DIR / "snapshots"
    32	# gzip 落盘：原始 payload 体量大（一条期权链 ~3MB），gzip 后 ~1/6，
    33	# 无损（仍是完整原始全字段），便于纳入 git 备份而不撑爆仓库。
    34	_SUFFIX = ".json.gz"
    35	
    36	
    37	class SnapshotStore:
    38	    def __init__(self, root: Path | None = None) -> None:
    39	        self.root = root or SNAPSHOT_DIR
    40	
    41	    def _dir(self, kind: str, symbol: str) -> Path:
    42	        return self.root / kind / symbol
    43	
    44	    def _path(self, kind: str, symbol: str, on_date: date) -> Path:
    45	        return self._dir(kind, symbol) / f"{on_date.isoformat()}{_SUFFIX}"
    46	
    47	    def save(self, kind: str, symbol: str, payload: Any, *, on_date: date,
    48	             captured_at: float | None = None) -> Path:
    49	        """落盘某标的某日的原始 payload（gzip 无损压缩）。同日重复保存会覆盖
    50	        （保留当日最新/最全的一份，日内 volume 累积，临近收盘那次最完整）。返回文件路径。"""
    51	        d = self._dir(kind, symbol)
    52	        d.mkdir(parents=True, exist_ok=True)
    53	        path = self._path(kind, symbol, on_date)
    54	        record = {
    55	            "captured_at": captured_at if captured_at is not None else time.time(),
    56	            "kind": kind,
    57	            "symbol": symbol,
    58	            "date": on_date.isoformat(),
    59	            "payload": payload,
    60	        }
    61	        blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
    62	        # ⚠️ 原子写：先写临时文件 → 验证能完整读回 → 才 rename 覆盖目标。
    63	        # 旧写法直接 gzip.open(path,"wb")，中途崩溃/磁盘满会留下**半截的坏文件**，
    64	        # 而 load() 又把坏文件静默折成 None —— 一份不可再生的期权链就这样没了，
    65	        # 且没有任何人知道（codex review 2026-08-28）。
    66	        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    67	        try:
    68	            with gzip.open(tmp, "wb") as f:
    69	                f.write(blob)
    70	            with gzip.open(tmp, "rb") as f:          # 回读验证，坏就不覆盖
    71	                json.loads(f.read().decode("utf-8"))
    72	            os.replace(tmp, path)
    73	        finally:
    74	            if tmp.exists():
    75	                try:
    76	                    tmp.unlink()
    77	                except OSError:
    78	                    pass
    79	        return path
    80	
    81	    def dates(self, kind: str, symbol: str) -> list[date]:
    82	        """该标的已落盘的所有日期（升序）。"""
    83	        d = self._dir(kind, symbol)
    84	        if not d.exists():
    85	            return []
    86	        out: list[date] = []
    87	        for p in d.glob(f"*{_SUFFIX}"):
    88	            stem = p.name[: -len(_SUFFIX)]
    89	            try:
    90	                out.append(date.fromisoformat(stem))
    91	            except ValueError:
    92	                continue
    93	        return sorted(out)
    94	
    95	    def load(self, kind: str, symbol: str, on_date: date, *,
    96	             quarantine: bool = True) -> Any | None:
    97	        """读回某日的原始 payload；文件不存在返回 None。
    98	
    99	        ⚠️ **损坏 ≠ 不存在**。旧写法把二者都折成 None，后果：
   100	          · 上层的"文件存在即算齐全"判据会把损坏文件当成有效快照；
     1	"""CBOE 期权数据源 —— 免费的延迟报价 JSON 接口。
     2	
     3	来源: https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json
     4	公开延迟数据（非 CME 那种禁止抓取的实时数据），每个行权价直接带
     5	open_interest / gamma / delta / iv，省去自己定价的麻烦（仍会用 BS 交叉校验）。
     6	
     7	注意：我们用 ETF 期权（GLD/SLV/USO）作为 COMEX 商品期权的【代理】——
     8	合法、可脚本化、但不是文章读的那张 COMEX 期权表。代理质量与换算见 config。
     9	后续若接入付费 COMEX 源，只需在此再加一个 source 实现，分析层不变。
    10	"""
    11	from __future__ import annotations
    12	
    13	from datetime import date, datetime
    14	
    15	from undertow.core.config import Instrument
    16	from undertow.collect.cache import FileCache
    17	from undertow.core.models import OptionContract, OptionsSnapshot
    18	from undertow.collect.base import DataSourceError, http_get_json
    19	
    20	CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    21	
    22	
    23	def parse_occ(symbol: str) -> tuple[str, date, str, float]:
    24	    """解析 OCC 期权代码，如 'GLD260918P00358000'。
    25	
    26	    从右往左切，兼容任意长度的 root:
    27	        最后 8 位 = 行权价 ×1000；前 1 位 = C/P；再前 6 位 = YYMMDD；其余 = root。
    28	    """
    29	    strike = int(symbol[-8:]) / 1000.0
    30	    kind = symbol[-9]
    31	    yymmdd = symbol[-15:-9]
    32	    root = symbol[:-15]
    33	    expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    34	    return root, expiry, kind, strike
    35	
    36	
    37	def _to_int(v) -> int:
    38	    try:
    39	        return int(float(v))
    40	    except (TypeError, ValueError):
    41	        return 0
    42	
    43	
    44	def _to_float(v) -> float:
    45	    try:
    46	        return float(v)
    47	    except (TypeError, ValueError):
    48	        return 0.0
    49	
    50	
    51	def snapshot_from_payload(payload: dict, instrument_key: str, sym: str) -> OptionsSnapshot:
    52	    """从 CBOE 原始 payload 还原 OptionsSnapshot（无 I/O）。
    53	
    54	    抽成自由函数，便于「快照仓库」把落盘的历史原始 payload 也还原成同一套模型，
    55	    供 flow 层做日对日 diff——不必再走网络。
    56	    """
    57	    data = payload.get("data") or {}
    58	    if "options" not in data:
    59	        raise DataSourceError(f"CBOE 返回无 options 字段（{sym}）")
    60	
    61	    spot = _to_float(data.get("current_price") or data.get("close"))
    62	    asof = payload.get("timestamp", "")
    63	
    64	    contracts: list[OptionContract] = []
    65	    for o in data["options"]:
    66	        try:
    67	            _root, expiry, kind, strike = parse_occ(o["option"])
    68	        except (ValueError, KeyError):
    69	            continue
    70	        contracts.append(OptionContract(
    71	            expiry=expiry,
    72	            strike=strike,
    73	            kind=kind,
    74	            open_interest=_to_int(o.get("open_interest")),
    75	            volume=_to_int(o.get("volume")),
    76	            gamma=_to_float(o.get("gamma")),
    77	            delta=_to_float(o.get("delta")),
    78	            iv=_to_float(o.get("iv")),
    79	            bid=_to_float(o.get("bid")),
    80	            ask=_to_float(o.get("ask")),
    81	            bid_size=_to_int(o.get("bid_size")),
    82	            ask_size=_to_int(o.get("ask_size")),
    83	        ))
    84	
    85	    return OptionsSnapshot(
    86	        instrument=instrument_key,
    87	        proxy_symbol=sym,
    88	        spot=spot,
    89	        asof=asof,
    90	        contracts=contracts,
    91	    )
    92	
    93	
    94	def chain_fingerprint(snap: OptionsSnapshot) -> str:
    95	    """期权**持仓结构**指纹（每行权价 expiry/kind/strike/OI），用于识别"无新持仓数据"。
    96	
    97	    只认 OI，**不含现价与 volume**——这是刻意的：
    98	    - 休市日（周末/节假日）：OI 不变 → 指纹相同 → 跳过，不把重复快照落成新的一天。
    99	    - 交易日 ET 凌晨 OCC 尚未发布隔夜结算 OI 时：CBOE 延迟报价里 OI 仍是上一交易日
   100	      的结算值，但现价/volume 已刷新成当日值。若指纹含现价/volume 就会误判为"新数据"
   101	      而落盘一份 OI 未结算的残缺快照（现价新、OI 旧），使 flow 层日对日 diff 退化成
   102	      全 0（ΔOI≡0）。只认 OI 后，这种"OI 未结算"状态会被正确识别为无新数据→跳过，
   103	      交给定时任务的后续重试点在 OCC 发布后再抓（见 scripts/daily_update.sh）。
   104	    我们的情报核心是持仓量（OI），现价另由期货源实时提供、volume 日内累积本就多变，
   105	    二者都不该参与"是否有新持仓"的判定。
   106	    """
   107	    import hashlib
   108	    # ⚠️ 只取 OI>0 的行。交易所每天都会新挂一批 OI=0 的行权价/到期，
   109	    # 若把它们计入指纹，"持仓完全没变"也会因为多了几百条空合约而哈希不同 →
   110	    # 放行落盘一份 OI 未结算的残缺快照，正是本函数声称要防的那种。
   111	    # 2026-08-27 实测：SPY 新增 388 条、IWM 新增 236 条，全部 OI=0，
   112	    # 而两者已有合约的总 |ΔOI| 恰为 0 —— 指纹却判定为"新数据"。
   113	    rows = sorted(
   114	        (c.expiry.isoformat(), c.kind, round(c.strike, 4), c.open_interest)
   115	        for c in snap.contracts if (c.open_interest or 0) > 0
   116	    )
   117	    h = hashlib.md5()
   118	    h.update(repr(rows).encode("utf-8"))
   119	    return h.hexdigest()
   120	
   121	
   122	def oi_change_total(prev: OptionsSnapshot, curr: OptionsSnapshot) -> int:
   123	    """两份快照之间【已建仓合约】的 OI 变动总量 Σ|ΔOI|（按 到期/类型/行权价 对齐）。
   124	
   125	    比 chain_fingerprint 更严格，用来判定"OCC 隔夜结算是否已落地"。
   126	    指纹是【单快照】函数，判不了两类情形：
   127	      1. 交易所新挂 OI=0 的行权价（已在指纹里排除）；
   128	      2. **合约到期消失** —— 存活合约的 OI 一张没动，但 OI>0 的行集合变了，
   129	         指纹照样不同 → 放行一份 OI 未结算的残缺快照。
   130	    2026-08-27 实测：GLD 在指纹修好之后仍因到期滚出而被放行，Σ|ΔOI| 恰为 0。
   131	
   132	    返回 0 表示【没有任何已建仓合约的持仓发生变化】＝ OI 尚未结算，不应落盘。
   133	    """
   134	    pm: dict = {}
   135	    for c in prev.contracts:
   136	        pm[(c.expiry, c.kind, round(c.strike, 4))] = c.open_interest or 0
   137	    total = 0
   138	    seen = set()
   139	    for c in curr.contracts:
   140	        k = (c.expiry, c.kind, round(c.strike, 4))
   141	        seen.add(k)
   142	        total += abs((c.open_interest or 0) - pm.get(k, 0))
   143	    # 消失的合约（到期滚出）不计入：它们的"归零"不是持仓变化，是合约不存在了
   144	    return total
   145	
   146	
   147	class CboeOptionsSource:
   148	    name = "cboe_etf"
   149	    # 延迟报价日内会变，但对"人工监控"30 分钟缓存够用；调试可 use_cache=False
   150	    CACHE_TTL = 30 * 60
   151	
   152	    def __init__(self, cache: FileCache | None = None) -> None:
   153	        self.cache = cache or FileCache()
   154	
   155	    def fetch_raw(self, instrument: Instrument, *, use_cache: bool = True) -> dict:
   156	        """取回 CBOE 原始 payload（全字段）。给「快照仓库」落盘用——
   157	        我们要尽量多存原始数据，而不是只存解析后的子集。"""
   158	        if instrument.options is None:
   159	            raise DataSourceError(f"{instrument.key} 未配置 options 数据源")
   160	        sym = instrument.options.symbol
   161	        cache_key = f"cboe_{sym}"
   162	
   163	        payload = self.cache.get(cache_key, self.CACHE_TTL if use_cache else 0) if use_cache else None
   164	        if payload is None:
   165	            payload = http_get_json(CBOE_URL.format(symbol=sym))
   166	            self.cache.set(cache_key, payload)
   167	        return payload
   168	
   169	    def fetch_snapshot(self, instrument: Instrument, *, use_cache: bool = True) -> OptionsSnapshot:
   170	        payload = self.fetch_raw(instrument, use_cache=use_cache)
   171	        return snapshot_from_payload(payload, instrument.key, instrument.options.symbol)
    90	    """全链 call/put 成交统计。"""
    91	    v = _run(["option", "volume", symbol])
    92	    return {"call": int(v.get("c", 0)), "put": int(v.get("p", 0))}
    93	
    94	
    95	def snapshot_near(symbol: str, *, band: float = 0.12, max_exp: int = 6) -> dict:
    96	    """近价带 + 近几个到期的链快照。
    97	
    98	    ⚠️ 长桥的 `option chain --date` 对【近月】到期直接返回
    99	    call_iv/call_vol/call_last + put_*（一次调用拿全档，快），
   100	    而对较远的到期只返回 call_symbol/put_symbol（要再逐个 quote，慢）。
   101	    实测分界在 9/4 与 9/18 之间。两种都要处理。
   102	    """
   103	    from undertow.collect.longbridge_quote import fetch_stock_quotes
   104	    spot = fetch_stock_quotes([symbol])[symbol].last
   105	    exps = expiries(symbol)[:max_exp]
   106	    rich, need_quote = [], []
   107	    for e in exps:
   108	        for row in strikes(symbol, e):
   109	            k = float(row.get("strike", 0) or 0)
   110	            if not (spot * (1 - band) <= k <= spot * (1 + band)):
   111	                continue
   112	            if "call_vol" in row:                     # 已带数据
   113	                rich.append({"expiry": e, "strike": k,
   114	                             "call_iv": float(row.get("call_iv") or 0),
   115	                             "call_vol": int(row.get("call_vol") or 0),
   116	                             "call_last": float(row.get("call_last") or 0),
   117	                             "put_iv": float(row.get("put_iv") or 0),
   118	                             "put_vol": int(row.get("put_vol") or 0),
   119	                             "put_last": float(row.get("put_last") or 0)})
   120	            elif "call_symbol" in row:                # 只有符号，需再查
   121	                need_quote += [row["call_symbol"], row["put_symbol"]]
   122	    q = quotes(need_quote) if need_quote else []
   123	    return {"symbol": symbol, "spot": spot, "expiries": exps,
   124	            "total_volume": total_volume(symbol),
   125	            "chain": rich, "quotes": q}
   126	
   127	
   128	def save(payload: dict, on_date: date | None = None) -> Path:
   129	    d = on_date or date.today()
   130	    p = STORE / payload["symbol"] / f"{d.isoformat()}.json"
   131	    p.parent.mkdir(parents=True, exist_ok=True)
   132	    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
   133	    return p
   134	
   135	
   136	def verify_lead(symbol: str, lb_date: date, cboe_date: date) -> dict:
   137	    """验证长桥是否真的领先一天。
   138	
   139	    比对：lb_date 当天存下的长桥成交量 vs cboe_date 快照里【描述同一交易日】的成交量。
   140	    若长桥领先一天，则 lb_date 存的应等于 cboe_date 快照描述的那天。
   141	    """
   142	    from undertow.cli import snapshot_from_payload
   143	    from undertow.collect.store import SnapshotStore
   144	    f = STORE / symbol / f"{lb_date.isoformat()}.json"
   145	    if not f.exists():

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "python3 -c \"import gzip,json,datetime; d=json.load(gzip.open('data/snapshots/options/GLD/2026-08-31.json.gz','rt')); print(d.keys()); print({k:d.get(k) for k in d if k"'!='"'payload'}); print(d['payload'].get('timestamp')); print(d['payload']['data']['current_price']); print(d['payload']['data']['options'][0])\"" in /Users/yhdong/Trading
 succeeded in 0ms:
python3: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
python3: error: couldn't create cache file '/tmp/xcrun_db-Z91tTJs7' (errno=Operation not permitted)
python3: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
python3: error: couldn't create cache file '/tmp/xcrun_db-2J4iTJLF' (errno=Operation not permitted)
dict_keys(['captured_at', 'kind', 'symbol', 'date', 'payload'])
{'captured_at': 1788170406.495074, 'kind': 'options', 'symbol': 'GLD', 'date': '2026-08-31'}
2026-08-31 09:42:34
407.23
{'option': 'GLD260831C00275000', 'bid': 132.15, 'bid_size': 10.0, 'ask': 135.7, 'ask_size': 10.0, 'iv': 1.8058, 'open_interest': 0.0, 'volume': 0.0, 'delta': 1.0, 'gamma': 0.0, 'vega': 0.0, 'theta': 0.0, 'rho': 0.0075, 'theo': 133.7877, 'change': 0.0, 'open': 0.0, 'high': 0.0, 'low': 0.0, 'tick': 'no_change', 'last_trade_price': 0.0, 'last_trade_time': None, 'percent_change': 0.0, 'prev_day_close': 134.375}

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "date -r 1788170406 '+%F %T %Z'; python3 -c \"import gzip,json;d=json.load(gzip.open('data/snapshots/options/GLD/2026-08-31.json.gz','rt')); xs=d['payload']['data']['options']; print([x for x in xs if x['option'].startswith('GLD260918P00400')][:3])\"" in /Users/yhdong/Trading
 succeeded in 0ms:
2026-08-31 18:00:06 +08
python3: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
python3: error: couldn't create cache file '/tmp/xcrun_db-rgTeqtzd' (errno=Operation not permitted)
python3: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
python3: error: couldn't create cache file '/tmp/xcrun_db-dtxQLOde' (errno=Operation not permitted)
[{'option': 'GLD260918P00400000', 'bid': 4.65, 'bid_size': 104.0, 'ask': 4.85, 'ask_size': 61.0, 'iv': 0.2251, 'open_interest': 20848.0, 'volume': 3207.0, 'delta': -0.323, 'gamma': 0.0164, 'vega': 0.3526, 'theta': -0.1914, 'rho': -0.063, 'theo': 4.7342, 'change': 2.26, 'open': 2.45, 'high': 5.35, 'low': 2.22, 'tick': 'up', 'last_trade_price': 4.73, 'last_trade_time': '2026-08-28T15:59:28', 'percent_change': 93.7759, 'prev_day_close': 4.625}]

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "git show b20505b -- undertow/analyze/flow.py undertow/analyze/outlook.py undertow/cli.py undertow/report/html.py | sed -n '1,460p'; nl -ba undertow/analyze/outlook.py | sed -n '220,310p'; rg -n \"tradeable|TRADEABLE|_ti\" undertow/analyze/outlook.py undertow/cli.py undertow/report/html.py" in /Users/yhdong/Trading
 succeeded in 0ms:
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-pvtOCuMD' (errno=Operation not permitted)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-jp86IGbC' (errno=Operation not permitted)
commit b20505b29f9b0881cc83f9fbd29d5deb4d637167
Author: YuhaoDoong <yuhaodong999@gmail.com>
Date:   Tue Sep 1 10:20:45 2026 +0800

    feat: 可交易信息闸门 —— 横盘日直接说「今天没信息」，不再照样给方向
    
    用户 2026-08-31：「不能每次出来一个，我想要照着做交易的时候，你又说这其实
    是不准的。现在的瓶颈到底是什么？是数据不够多吗？」
    
    ## 根因不是数据量，是横盘日把统计稀释了
    
    开火信号 26 笔配实际价格，按当日实际波动幅度分组：
    
      横盘 <0.5%      6/12 =  50%   平均 +0.01%   ← 掷硬币
      小动 0.5~1.5%   7/10 =  70%   平均 +0.26%
      中动 1.5~3%      3/3 = 100%   平均 +1.85%
      大动 >3%         1/1 = 100%   平均 +3.16%
      全样本          17/26 = 65%   平均 +0.44%/笔   p=0.169
    
    65% 这个实测值只需 n=47 就能显著，现在 26，还差 21 个（约 2~3 个月）。
    此前我用假设的 55% 去论证「需要 3.1 年、验证不了」，是拿悲观假设否定了
    比它好得多的实测值 —— 「不显著」的正确含义是「还不能下结论」，不是「无效」。
    
    ## 闸门：用盘前已知的压力倍数识别无信息日
    
    91 品种-日 / 36 日期簇，快照 D 盘前 → 当日 open→close：
    
      闸门            拦掉  放行  拦掉组  放行组  放行顺向  Fisher p
      可判定率 <30%     16   75    38%    61%   +0.37%   0.099
      压力倍数 <1.5×    15   76    33%    62%   +0.40%   0.050
      压力倍数 <2×      29   62    41%    65%   +0.46%   0.044  ←
      压力倍数 <3×      47   44    51%    64%   +0.44%   0.290
    
    选 2×：p 最小，且放行组胜率在 2× 之后稳定在 64~65%（拐点而非单点异常），
    2 倍本身也有先验合理性。
    
    ⚠️ 共测 10 个阈值，Bonferroni 校正后 p=0.44 不再显著。这个事实写进
    GATE_EVIDENCE 且由测试锁住，报告横幅必须连同样本量一起显示。
    ⚠️ 拦掉组 41% 不代表可反向操作（n=29，与 50% 差异本身不显著），
    渲染文案显式否掉这个误读，测试断言该否定必须存在。
    
    ## 改动
    
    - flow.tradeable_info()：返回 tradeable/ratio/decidable/side/reason/evidence，
      只用盘前已知量，不含任何当日价格信息
    - html.render_tradeable_gate()：横幅置于报告最顶（在强信号横幅之前）——
      它决定下面所有方向结论该给多少信任
    - 可判定率 <30% 时附加警告（今天 SLV 20%：61,022 张增仓里 90% 判不出买卖方，
      IV 齐落时买方腿被闸门掐掉，倍数是在很小的分子分母上算的）
    
    ## 今日实测
    
      GLD   ✅  5.7×  可判定率 84%  看涨   ← 全品种可判定率最高
      SLV   ✅ 50.3×  可判定率 20%  看跌   ← 触发低可判定率警告
      QQQ   ⛔  1.1×  可判定率 50%        ← 拦下。此前是靠人工判断「先放着」
      TQQQ  ✅  8.2×  可判定率 51%  看涨
    
    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
    Claude-Session: https://claude.ai/code/session_015nR6EdboHusPmkZFVGNhwZ

diff --git a/undertow/analyze/flow.py b/undertow/analyze/flow.py
index 4ef4b76..1c21bd0 100644
--- a/undertow/analyze/flow.py
+++ b/undertow/analyze/flow.py
@@ -2076,3 +2076,85 @@ def wall_structure(fa: "FlowAnalysis", wall: float | None,
             if (_wiv(below) is not None and _wiv(at) is not None) else None),
     }
 
+
+
+# ─────────────────────────────────────────────────────────────────────────────
+# 可交易信息闸门（2026-08-31 加）
+#
+# 起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，你又说
+# 这其实是不准的」。查下去发现整体统计不显著是被【横盘日】稀释的：
+#
+#   开火信号 26 笔，按当日实际波动分组
+#     横盘 <0.5%      6/12 =  50%   平均 +0.01%   ← 掷硬币
+#     小动 0.5~1.5%   7/10 =  70%   平均 +0.26%
+#     中动 1.5~3%      3/3 = 100%   平均 +1.85%
+#     大动 >3%         1/1 = 100%   平均 +3.16%
+#
+# 但"今天会不会有行情"事后才知道。所以要找一个【盘前已知】的代理。
+# 测了两族候选（91 品种-日 / 36 日期簇，快照 D 盘前 → 当日 open→close）：
+#
+#   闸门            拦掉  放行  拦掉组胜率  放行组胜率  放行顺向  Fisher p
+#   可判定率 <30%     16   75      38%       61%     +0.37%   0.099
+#   压力倍数 <1.5×    15   76      33%       62%     +0.40%   0.050
+#   压力倍数 <2×      29   62      41%       65%     +0.46%   0.044  ←
+#   压力倍数 <3×      47   44      51%       64%     +0.44%   0.290
+#
+# 选 2×：p 最小，且放行组胜率在 2× 之后稳定在 64~65%（不是单点异常，是拐点）；
+# 2 倍本身也有先验合理性，不是从数据里挖出来的怪阈值。
+#
+# ⚠️ 诚实标注：测了 10 个阈值，Bonferroni 校正后 0.044×10 = 0.44，不再显著。
+#    这个闸门需要样本外验证才算数 —— 报告里必须连同样本量一起显示，
+#    不得呈现为已确证的规则。
+# ⚠️ 拦掉组胜率 41%（低于 50%）不代表可以反着做：n=29，且 41% 与 50% 的差异
+#    本身不显著。正确读法是"这些日子没信息"，不是"这些日子反向有信息"。
+# ─────────────────────────────────────────────────────────────────────────────
+
+TRADEABLE_MIN_RATIO = 2.0     # 压力倍数低于此 → 不出方向结论
+TRADEABLE_MIN_DECIDABLE = 0.30  # 可判定率低于此 → 附加警告（p=0.099，未达显著）
+
+# 闸门的实测表现，报告直接引用，不得只写结论不写样本
+GATE_EVIDENCE = {
+    "n_pairs": 91, "n_clusters": 36,
+    "blocked_n": 29, "blocked_hit": 0.41, "blocked_ret": -0.12,
+    "passed_n": 62, "passed_hit": 0.65, "passed_ret": +0.46,
+    "fisher_p": 0.044, "bonferroni_p": 0.44, "n_thresholds_tested": 10,
+}
+
+
+def tradeable_info(fa) -> dict:
+    """今天这个品种的资金流里有没有可交易信息。
+
+    返回 dict：tradeable(bool) / ratio / decidable / reason / evidence
+    只看【盘前已知】的量（增仓结构），不含任何当日价格信息。
+    """
+    inc = [c for c in fa.changes if c.d_oi > 0]
+    dn = sum(c.d_oi for c in inc if c.bias == "bearish")
+    up = sum(c.d_oi for c in inc if c.bias == "bullish")
+    nu = sum(c.d_oi for c in inc if c.bias == "neutral")
+    total = dn + up + nu
+    if total <= 0:
+        return {"tradeable": False, "ratio": 0.0, "decidable": 0.0,
+                "reason": "无增仓，无从判断", "evidence": GATE_EVIDENCE}
+    decidable = (dn + up) / total
+    hi, lo = max(dn, up), min(dn, up)
+    ratio = hi / lo if lo > 0 else (float("inf") if hi > 0 else 0.0)
+    side = "看跌" if dn > up else ("看涨" if up > dn else "无倾向")
+    if ratio < TRADEABLE_MIN_RATIO:
+        return {"tradeable": False, "ratio": ratio, "decidable": decidable,
+                "side": side,
+                "reason": (f"多空压力比 {ratio:.1f}×，低于 {TRADEABLE_MIN_RATIO:g}× 闸门"
+                           f"（看跌 {dn:,} / 看涨 {up:,}）—— 这不是分歧信号，"
+                           f"是**没有信息**。实测这类日子胜率 "
+                           f"{GATE_EVIDENCE['blocked_hit']:.0%}、"
+                           f"顺向收益 {GATE_EVIDENCE['blocked_ret']:+.2f}%。"),
+                "evidence": GATE_EVIDENCE}
+    warn = ""
+    if decidable < TRADEABLE_MIN_DECIDABLE:
+        warn = (f"　⚠️ 但可判定率仅 {decidable:.0%}（{nu:,}/{total:,} 张判不出买卖方，"
+                f"多因 IV 齐落时买方腿被闸门掐掉）——倍数是在很小的分子分母上算的。")
+    return {"tradeable": True, "ratio": ratio, "decidable": decidable, "side": side,
+            "reason": (f"多空压力比 {ratio:.1f}×（看跌 {dn:,} / 看涨 {up:,}），"
+                       f"过 {TRADEABLE_MIN_RATIO:g}× 闸门。实测这类日子胜率 "
+                       f"{GATE_EVIDENCE['passed_hit']:.0%}、"
+                       f"顺向 {GATE_EVIDENCE['passed_ret']:+.2f}%/笔。{warn}"),
+            "evidence": GATE_EVIDENCE}
diff --git a/undertow/cli.py b/undertow/cli.py
index c22c86e..82743ad 100644
--- a/undertow/cli.py
+++ b/undertow/cli.py
@@ -36,7 +36,7 @@ from undertow.analyze.signals import generate_signals, net_bias
 from undertow.analyze.gamma import (analyze_gamma, structure_delta,
                                    support_ladder, ladder_bands, wall_agreement)
 from undertow.analyze.flow import _live as _flow_live
-from undertow.analyze.flow import (analyze_flow, counter_signals,
+from undertow.analyze.flow import (analyze_flow, counter_signals, tradeable_info,
                                    flip_driver_summary, structural_moves,
                                    detect_strong_signal, probe_strong_signal)
 from undertow.analyze.outlook import (build_outlook, macro_to_votes,
@@ -54,6 +54,7 @@ from undertow.analyze.family import check as _family_check
 from undertow.analyze.indicators import build as _build_labels
 from undertow.report.html import (render_report_html, render_index_html,
                           render_wall_layers_section,
+                          render_tradeable_gate,
                           render_flow_section, render_macro_section, render_events_section,
                           render_tldr_section, render_strategy_section,
                           render_concentration_html, render_vol_regime_section,
@@ -1357,6 +1358,14 @@ def cmd_report(args) -> int:
             strategy_html = (render_strategy_hub(strategy_props) + strategy_html
                              + render_credit_spread_section(cs_plan)
                              + render_condor_section(condor_plan))
+            # —— 可交易信息闸门（压力倍数 <2× = 今天没信息，见 flow.tradeable_info）——
+            gate_html = ""
+            try:
+                _ti = tradeable_info(fa)
+                gate_html = render_tradeable_gate(_ti, inst.display_name)
+            except Exception as e:
+                print(f"⚠️ {inst.key} 可交易闸门失败：{type(e).__name__}: {e}", file=sys.stderr)
+
             # —— 期权结构按到期分层（近端置顶）——
             # 主报告的墙来自 analyze_gamma 的跨到期加总，会造出实盘不存在的位置；
             # 这一节把它拆回近/中/远三层，并标出近端与中端是否指向同一位置。
@@ -1510,7 +1519,8 @@ def cmd_report(args) -> int:
                                       indicators_html=_indicators_html,
                                       expiry_html2=_expiry_html,
                                       summary_html=_summary_html,
-                                      layers_html=layers_html)
+                                      layers_html=layers_html,
+                                      gate_html=gate_html)
             # ⚠️ 文件名用【可交易日】（= 快照日期），不是生成日期。
             # 时点约定：快照 D 于 D 凌晨捕获，OI 是 D−1 收盘的 OCC 结算，
             # diff 描述交易日 D−1，**D 开盘才可执行** —— D 就是这份研报的身份。
diff --git a/undertow/report/html.py b/undertow/report/html.py
index 3639e65..68844c4 100644
--- a/undertow/report/html.py
+++ b/undertow/report/html.py
@@ -1397,7 +1397,7 @@ def render_report_html(o: Outlook, price_svg: str, oi_svg: str, cot_svg: str,
                        stretch_read=None, vintage_html: str = "",
                        indicators_html: str = "",
                        expiry_html2: str = "", summary_html: str = "",
-                       layers_html: str = "") -> str:
+                       layers_html: str = "", gate_html: str = "") -> str:
     if o.commodity_symbol and o.commodity_spot is not None:
         # 真实期货价为主，ETF 代理为辅
         price_line = (f'真实价 <b>{o.commodity_spot:,.1f}</b>（{_esc(o.commodity_symbol)} 期货）'
@@ -1418,6 +1418,11 @@ def render_report_html(o: Outlook, price_svg: str, oi_svg: str, cot_svg: str,
         + f'<div class="sub">环境：{_esc(o.regime)}</div></div>'
     )
     body = (
+        # 可交易信息闸门排在最前 —— 它决定下面所有方向结论该给多少信任。
+        # 横盘日信号胜率 50%，有行情日 70~100%；不先说清今天属于哪种，
+        # 后面的「⚡极强看跌」会被当成可执行结论（用户 2026-08-31 的原话：
+        # 「照着做交易的时候，你又说这其实是不准的」）。
+        f'{gate_html}'
         f'{strong_html}'
         # ── 板块顺序（用户 2026-08-29 指定，2026-08-31 在最前面插入分层）──────
         #   ① 期权结构按到期分层  ② 期权关键点位  ③ 综合研判  ④ 大白话  ⑤ 增仓按到期拆开
@@ -2143,3 +2148,45 @@ def render_wall_layers_section(ga, ladder=None, bands=None, agree=None,
         + '<div class="sub" style="margin-top:10px">墙 = 该层该侧 OI 最大的行权价（现价 ±15% 内）。'
           'OI 是持仓不是方向，厚墙只说明"很多人在这有仓"，不等于价格一定停在那。</div></div>'
     )
+
+
+def render_tradeable_gate(ti, display_name: str = "") -> str:
+    """可交易信息闸门横幅 —— 放在报告最顶，决定下面所有方向结论该给多少信任。
+
+    起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，
+    你又说这其实是不准的」。横盘日信号胜率 50%（掷硬币），有行情日 70~100%，
+    整体不显著是被横盘日稀释的。这个闸门用盘前已知的压力倍数把那类日子标出来。
+    """
+    if not ti:
+        return ""
+    ev = ti.get("evidence", {})
+    ok = ti.get("tradeable")
+    ratio = ti.get("ratio", 0.0)
+    rtxt = "∞" if ratio == float("inf") else f"{ratio:.1f}×"
+    if ok:
+        bg, bd, fg, icon, head = "#1a7f370d", "#1a7f37", "#1a7f37", "✅", "今天有可交易信息"
+    else:
+        bg, bd, fg, icon, head = "#bc4c000d", "#bc4c00", "#bc4c00", "⛔", "今天没有可交易信息"
+    # 证据行：样本量必须和结论同时出现，不得只写结论
+    eb = ""
+    if ev:
+        eb = (f'<div class="sub" style="margin-top:8px;line-height:1.7">'
+              f'闸门实测（{ev.get("n_pairs")} 品种-日 / {ev.get("n_clusters")} 个日期簇，'
+              f'快照盘前 → 当日开盘→收盘）：'
+              f'<b>放行组 {ev.get("passed_n")} 笔胜率 {ev.get("passed_hit",0):.0%}、'
+              f'顺向 {ev.get("passed_ret",0):+.2f}%/笔</b>；'
+              f'拦掉组 {ev.get("blocked_n")} 笔胜率 {ev.get("blocked_hit",0):.0%}、'
+              f'顺向 {ev.get("blocked_ret",0):+.2f}%。Fisher p={ev.get("fisher_p")}。'
+              f'<br><span style="color:#bc4c00">⚠️ 共测了 {ev.get("n_thresholds_tested")} 个阈值，'
+              f'Bonferroni 校正后 p={ev.get("bonferroni_p")} 不再显著 —— '
+              f'这个闸门尚待样本外验证，不是已确证的规则。</span>'
+              f'　拦掉组 41% 不代表可以反着做（n=29，与 50% 的差异本身不显著），'
+              f'正确读法是「这些日子没信息」。</div>')
+    return (f'<div class="card" style="background:{bg};border-left:4px solid {bd}">'
+            f'<h2 style="margin-top:0;color:{fg}">{icon} {_esc(head)}'
+            f'{" · " + _esc(display_name) if display_name else ""}</h2>'
+            f'<div style="font-size:15px;margin:6px 0"><b>多空压力比 {rtxt}</b>'
+            f'　·　可判定率 {ti.get("decidable",0):.0%}'
+            f'　·　倾向 {_esc(ti.get("side","—"))}</div>'
+            f'<div class="sub" style="line-height:1.7">{_esc(ti.get("reason",""))}</div>'
+            f'{eb}</div>')
   220	def plain_summary(o: Outlook, **kw) -> str:
   221	    """大白话速读（单段文本版）：分块内容顺序连写，供终端/测试等纯文本场景。"""
   222	    return "".join(txt for _, txt in plain_summary_blocks(o, **kw))
   223	
   224	
   225	def macro_to_votes(ma) -> list[FactorVote]:
   226	    """把宏观驱动（实际利率/美元/通胀预期）转成方向因子票。"""
   227	    out: list[FactorVote] = []
   228	    for d in ma.drivers:
   229	        if d.vote_sign == 0:
   230	            continue
   231	        out.append(FactorVote(
   232	            layer="Macro", factor=d.name, direction=_DIR_CN[d.vote_sign],
   233	            sign=d.vote_sign, weight=round(d.weight, 2),
   234	            reliability=d.reliability, detail=d.detail,
   235	        ))
   236	    return out
   237	
   238	
   239	def _cot_votes(signals: list[Signal]) -> list[FactorVote]:
   240	    out: list[FactorVote] = []
   241	    for s in signals:
   242	        sign = _DIR_SIGN.get(s.direction, 0)
   243	        rel_w, rel_lbl, _note = SIGNAL_RELIABILITY.get(s.code, (0.8, "中", ""))
   244	        w = _STRENGTH_W.get(s.strength, 1.0) * rel_w * (1 if sign else 0)
   245	        out.append(FactorVote(
   246	            layer="COT", factor=s.title, direction=_DIR_CN[sign], sign=sign,
   247	            weight=round(w, 2), reliability=rel_lbl, detail=s.detail.split("。")[0] + "。",
   248	        ))
   249	    return out
   250	
   251	
   252	def _gamma_vote(ga: GammaAnalysis) -> list[FactorVote]:
   253	    out: list[FactorVote] = []
   254	    # 墙位空间：现价距 call 墙(上方阻力/磁吸) vs 距 put 墙(下方支撑) 的相对空间
   255	    if ga.call_wall_oi > 0 and ga.put_wall_oi > 0 and ga.spot > 0:
   256	        up_room = (ga.call_wall - ga.spot) / ga.spot
   257	        down_room = (ga.spot - ga.put_wall) / ga.spot
   258	        diff = up_room - down_room
   259	        sign = 1 if diff > 0.02 else (-1 if diff < -0.02 else 0)
   260	        # ⚠️ 2026-08-31 回测：该因子在任何口径下都没有预测力。
   261	        #   旧 45 天混算 + 差值±2%   54%（38/70）
   262	        #   新 近端≤14天 + 差值±2%   49%（41/83）
   263	        #   新 近端≤14天 + 比值1.5   49%（37/75）
   264	        #   基准(无脑做多) 51%，43 个日期簇、四个变体 binomial p 全 = 1.000。
   265	        # 墙位口径已按用户 2026-08-31 决定改用近端（显示层确实更正确：混算会造出
   266	        # 实盘不存在的墙），但换口径并没有让这个因子变准 —— 它本来就不准。
   267	        # weight 保持 0.6 未动：降权会改变综合评分，属用户决定，不由这里擅自改。
   268	        out.append(FactorVote(
   269	            layer="Gamma", factor="墙位空间", direction=_DIR_CN[sign], sign=sign,
   270	            weight=round(0.6 * (1 if sign else 0), 2), reliability="低·未通过回测",
   271	            detail=(f"上行至 call 墙 {ga.call_wall:.1f} 空间 {up_room*100:+.1f}%、"
   272	                    f"下行至 put 墙 {ga.put_wall:.1f} 空间 {down_room*100:+.1f}%（近端 ≤14天 口径）。"
   273	                    f"⚠️ 该因子回测命中 49%（近端）/ 54%（混算），基准 51%，"
   274	                    f"43 个日期簇 p=1.000 —— 与掷硬币无异，看看即可，别当依据。"),
   275	        ))
   276	    # Put/Call OI 比极端 → 反指（情绪过度）
   277	    pcr = ga.put_call_ratio
   278	    if pcr == pcr:  # 非 NaN
   279	        sign = 1 if pcr >= 1.6 else (-1 if pcr <= 0.6 else 0)
   280	        if sign:
   281	            out.append(FactorVote(
   282	                layer="Gamma", factor="Put/Call OI 比极端", direction=_DIR_CN[sign], sign=sign,
   283	                weight=0.5, reliability="低",
   284	                detail=f"P/C OI 比 {pcr:.2f}（过度{'看跌→反指偏多' if sign>0 else '看涨→反指偏空'}）。",
   285	            ))
   286	    return out
   287	
   288	
   289	def _flow_vote(fa: FlowAnalysis) -> list[FactorVote]:
   290	    if fa.prev_date:  # 有日对日 diff：用买卖方加权压力（与报告里的净倾向一致）
   291	        dn, up = fa.downside_pressure, fa.upside_pressure
   292	        sign = 0
   293	        if dn > up * 1.3:
   294	            sign = -1
   295	        elif up > dn * 1.3:
   296	            sign = 1
   297	        if sign:
   298	            return [FactorVote(
   299	                layer="Flow", factor="买卖方资金流", direction=_DIR_CN[sign], sign=sign,
   300	                weight=0.8, reliability="中",
   301	                detail=f"买卖方加权 看跌加权增仓 {dn:,.0f} vs 看涨 {up:,.0f}"
   302	                       f"（OI增 × IV方向判买卖方；非「上方阻力」，是推动价格的加权增仓量）。",
   303	            )]
   304	        return []
   305	    # 仅单快照：用今日成交 put/call 比（弱）
   306	    tcv, tpv = fa.total_call_volume, fa.total_put_volume
   307	    if tcv > 0:
   308	        ratio = tpv / tcv
   309	        sign = -1 if ratio >= 1.4 else (1 if ratio <= 0.7 else 0)
   310	        if sign:
undertow/analyze/outlook.py:102:                         vol_verdict: str = "", flow_tilt: str = "",
undertow/analyze/outlook.py:203:    if flow_tilt:
undertow/analyze/outlook.py:204:        bits.append(f"资金流净倾向{flow_tilt}")
undertow/report/html.py:539:    tilt = f'<div class="sub">净倾向：<b>{_esc(fa.flow_tilt)}</b></div>'
undertow/report/html.py:596:def _tilt_color(tilt: str) -> str:
undertow/report/html.py:679:            tcol = _tilt_color(s.flow_tilt)
undertow/report/html.py:681:                    f'<b style="color:{tcol}">{_esc(s.flow_tilt)}</b></div>'
undertow/report/html.py:2211:def render_tradeable_gate(ti, display_name: str = "") -> str:
undertow/report/html.py:2221:    ok = ti.get("tradeable")
undertow/cli.py:42:from undertow.analyze.flow import (analyze_flow, counter_signals, tradeable_info, detect_ratio_spreads,
undertow/cli.py:60:                          render_tradeable_gate,
undertow/cli.py:353:            from undertow.analyze.flow import tradeable_info as _ti_fn
undertow/cli.py:354:            _t = _ti_fn(fa)
undertow/cli.py:357:                for _tier in ("conservative", "aggressive"):
undertow/cli.py:358:                    _v = _cwp(snap_curr, ref, spot, _t["side"], _t["ratio"], tier=_tier)
undertow/cli.py:361:                        _cw[_tier] = {"sell": _s0.sell_strike, "buy": _s0.buy_strike,
undertow/cli.py:365:                    elif not _v.ok and _tier == "conservative":
undertow/cli.py:1226:                px_title = f"真实期货价日线 + 关键位（{real_series.symbol}）"
undertow/cli.py:1230:                px_title = f"价格日线 + 关键位（{price.symbol} ETF）"
undertow/cli.py:1231:            price_svg = viz.price_levels_svg(px_dates, px_closes, levels, px_spot, title=px_title)
undertow/cli.py:1297:            timeline_svg = viz.strategy_timeline_svg(timeline_rows, real_price) \
undertow/cli.py:1329:            tilt = fa.flow_tilt if not fa.flow_tilt.startswith("—") else ""
undertow/cli.py:1365:                flow_tilt=tilt, flow_moves=moves, counter_notes=counters,
undertow/cli.py:1405:            # —— 可交易信息闸门（压力倍数 <2× = 今天没信息，见 flow.tradeable_info）——
undertow/cli.py:1408:                _ti = tradeable_info(fa)
undertow/cli.py:1409:                gate_html = render_tradeable_gate(_ti, inst.display_name)
undertow/cli.py:1437:                if _ti and _ti.get("side") in ("看涨", "看跌"):
undertow/cli.py:1445:                    _vs = {t: cw_propose(curr, obs_day, curr.spot, _ti["side"],
undertow/cli.py:1446:                                         _ti["ratio"], tier=t)
undertow/cli.py:1459:                if _ti and _ti.get("side") in ("看涨", "看跌"):
undertow/cli.py:1460:                    _cands = cost_candidates(curr, curr.spot, _ti["side"], obs_day,
undertow/cli.py:1461:                                             decidable=_ti["decidable"])
undertow/cli.py:1463:                        _cands, spot=curr.spot, side=_ti["side"],

hook: PostToolUse
hook: PostToolUse Completed
codex
时序核对也确认了一个关键错位：`2026-08-31` 快照是在美股开盘前保存，合约 `last_trade_time` 仍停在上周五；因此所谓“盘前真实 bid/ask 建仓”其实不是周一开盘可成交价。与此同时，候选 DTE 用的是 `obs_day`（前一交易日），会把实际 0DTE 算成 1DTE，并整体错一日。
web search: 
web search: site:theocc.com characteristics risks standardized options early assignment spread pin risk expiration ...
hook: PreToolUse
hook: PreToolUse Completed
hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "nl -ba undertow/report/html.py | sed -n '2250,2325p'; nl -ba undertow/cli.py | sed -n '1128,1155p'; nl -ba undertow/cli.py | sed -n '330,375p'; nl -ba undertow/analyze/validation.py | sed -n '72,180p'" in /Users/yhdong/Trading
 succeeded in 0ms:
  2250	            f'{eb}</div>')
  2251	
  2252	
  2253	def render_cost_gate(rows, exp_move=None, spot: float = 0.0, side: str = "",
  2254	                     conv=None, unit: str = "", etf_symbol: str = "") -> str:
  2255	    """成本闸门：这天大概走多少 vs 这张要走多少才不亏，并排放。
  2256	
  2257	    起因（用户 2026-08-31 那晚四笔全亏，方向却基本都对）：
  2258	      SLV 9/2 60P 需标的跌 1.9% 才回本，当天只跌 0.75% → 方向对也白做。
  2259	      GLD 410C 持有 1 分钟割掉亏 11%，那 11% 就是点差本身。
  2260	    只给方向不给这两个数，等于让人拿着对的判断去买错的合约。
  2261	    """
  2262	    if not rows:
  2263	        return ""
  2264	    em = exp_move or rows[0][1].exp_move
  2265	    sym = _esc(etf_symbol)
  2266	
  2267	    def _p(v):
  2268	        return f"{(conv(v) if conv else v):.1f}{_esc(unit)}"
  2269	
  2270	    trs = []
  2271	    any_ok = False
  2272	    for be, v in rows:
  2273	        any_ok = any_ok or v.ok
  2274	        c = "#1a7f37" if v.ok else "#bc4c00"
  2275	        icon = "✅" if v.ok else "⛔"
  2276	        theta_warn = (' <span style="color:#bc4c00">拿不过夜</span>'
  2277	                      if be.theta_share > 0.25 else "")
  2278	        trs.append(
  2279	            f'<tr><td>{_p(be.strike)}{be.kind}'
  2280	            + (f' <span style="color:#0969da">{sym}{be.strike:g}</span>' if conv else "")
  2281	            + f'</td><td>{be.expiry:%m/%d}<span class="sub">（{be.dte}天）</span></td>'
  2282	            f'<td class="r">${be.cost:,.0f}</td>'
  2283	            f'<td class="r">{be.spread_pct:.0%}</td>'
  2284	            f'<td class="r">{be.delta:+.2f}</td>'
  2285	            f'<td class="r">{be.theta_share:.0%}{theta_warn}</td>'
  2286	            f'<td class="r" style="color:{c};font-weight:600">{v.need_pct:.2f}%</td>'
  2287	            f'<td style="color:{c}">{icon}</td></tr>')
  2288	
  2289	    head_c = "#1a7f37" if any_ok else "#bc4c00"
  2290	    head_t = ("这天的典型幅度够覆盖成本" if any_ok
  2291	              else "这天的典型幅度覆盖不了任何一张的成本")
  2292	    weak = ('<span style="color:#bc4c00">（样本仅 %d 笔，只作量级参考）</span>' % em.n
  2293	            if em.weak else f'（n={em.n}）')
  2294	    return (
  2295	        '<div class="card"><h2>成本闸门 · 这天能走多少 vs 这张要走多少</h2>'
  2296	        f'<div style="font-size:15px;margin:6px 0;color:{head_c}"><b>{_esc(head_t)}</b></div>'
  2297	        f'<div class="sub" style="line-height:1.8">'
  2298	        f'<b>预期波动 {em.pct:.2f}%</b>　—— 可判定率落在 {_esc(em.band)} 档 {weak}，'
  2299	        f'该档 {em.p_over_1pct:.0%} 的日子波动超过 1%。'
  2300	        f'<br>可判定率预告的是<b>幅度</b>不是方向：实测 r=+0.243（对波动）'
  2301	        f' vs r=+0.053（对命中率）—— 高低两组命中率一样（65% vs 66%），'
  2302	        f'波动差一倍（0.55% vs 1.09%）。</div>'
  2303	        '<table style="margin-top:10px"><tr><th>合约</th><th>到期</th><th>一张</th>'
  2304	        '<th>点差</th><th>Δ</th><th>θ/权利金</th><th>回本需走</th><th></th></tr>'
  2305	        + "".join(trs) + '</table>'
  2306	        '<div class="sub" style="margin-top:8px;line-height:1.7">'
  2307	        '回本需走 =（点差 + |θ|×持有天数 + 手续费/100）÷|Δ|÷现价，即标的要往'
  2308	        f'{_esc(side) or "顺向"}走多少，这笔才刚好不亏。用快照盘口（盘前已知），'
  2309	        '实际下单时点差可能不同。<br>'
  2310	        '<b>θ/权利金</b> 是每天损耗掉的比例——超过 25% 的基本拿不过夜，'
  2311	        '近月便宜的代价全在这一栏。<br>'
  2312	        '⚠️ 预期波动表是在同一批数据上的第二次找模式（先测倍数闸门、再测可判定率），'
  2313	        '多重比较风险仍在，需样本外验证。方向对了也可能因为幅度不够而亏，'
  2314	        '这张表只解决幅度，不解决方向。</div></div>')
  2315	
  2316	
  2317	def render_backmonth(scan, spot: float = 0.0, conv=None, unit: str = "",
  2318	                     etf_symbol: str = "") -> str:
  2319	    """远月结构异动卡（playbook R16）。
  2320	
  2321	    【时间尺度隔离纪律】这张卡只作长期背景：不进综合分、不进日度方向研判、
  2322	    不改任何近月位点。机构完全可以一边持有远月上行尾部、一边在近月做空 ——
  2323	    两个时间尺度的仓位并存不矛盾。标注文字是硬要求，由测试锁住。
  2324	    """
  2325	    if scan is None or scan.empty:
  1128	            # _drop_incomplete_bar 只剔"≥today 的最后一根"，实时跑时刚好对（today 那根
  1129	            # 是盘中未完成的）；但回放时序列早已长到 today 之后，只剔一根会留下 today
  1130	            # 当天的收盘价 —— 那是当时还不存在的信息。
  1131	            # 2026-08-29 实测：回放 8/28 的黄金，因为吃进了 8/28 当天 −3.24% 的收盘价，
  1132	            # 超买超卖从当时真实的"偏超买 78%"变成"中性 57%"，直接改写了结论。
  1133	            if replay and real_series is not None and real_series.dates:
  1134	                real_series, real_price = _truncate_before(real_series, today), None
  1135	                if real_series.closes:
  1136	                    real_price = real_series.closes[-1]
  1137	                    ratio = (real_price / curr.spot) if curr.spot > 0 else None
  1138	            mult = ratio if ratio is not None else inst.options.approx_commodity_multiplier
  1139	
  1140	            # 观察日 = 链交易日（快照日前一工作日）：报告的期权结构是"昨收快照"，
  1141	            # 计时按观察日锚定——否则当日到期(0DTE)合约会被当成已过期剔除，
  1142	            # 而自动报告恰在 ET 凌晨生成、这些合约当天仍在交易（Codex P0-4）
  1143	            obs_day = _prev_weekday(date.fromisoformat(curr_date_s)) if curr_date_s else _prev_weekday(today)
  1144	
  1145	            ga = analyze_gamma(curr, multiplier=mult,
  1146	                               proxy_quality=inst.options.proxy_quality, today=obs_day,
  1147	                               horizon_days=args.horizon)
  1148	            fa = analyze_flow(prev, curr, today=obs_day, horizon_days=args.horizon,
  1149	                              call_wall=ga.call_wall, put_wall=ga.put_wall,
  1150	                              prev_date=prev_date, curr_date=curr_date_s)
  1151	            if ratio is not None:
  1152	                etf_sym = inst.options.symbol
  1153	                fut_sym = inst.commodity.symbol
  1154	                basis = f"实时比值 {fut_sym}/{etf_sym}={ratio:.3f}（{real_asof[:10]}）"
  1155	                comm_sym = fut_sym
   330	        # 远月看涨"这类结构（用户 2026-08-29 追问）
   331	        from undertow.analyze.flow import (expiry_split, expiry_split_conflict,
   332	                                            dominant_expiry)
   333	        _sp = expiry_split(fa)
   334	        out["exp_split"] = _sp
   335	        out["exp_conflict"] = expiry_split_conflict(_sp)
   336	        from undertow.analyze.flow import dte_agreement as _dte_agree
   337	        out["exp_agreement"] = _dte_agree(_sp)   # agree/conflict/insufficient 三态
   338	        # 主力到期：由数据决定哪个到期日占大头，不预先划桶（用户 2026-08-29）
   339	        out["dominant"] = dominant_expiry(ch, getattr(fa, "curr_date", None))
   340	        # 保护迁移的结构描述 —— 只陈述"钱从哪撤到哪、哪档涨价最急"，不做预测。
   341	        # 这是用户 2026-08-29 点名要置顶高亮的那段描述。
   342	        try:
   343	            from undertow.analyze.flow import wall_structure
   344	            pw_ = getattr(ga, "put_wall", None) if ga is not None else None
   345	            # wall_structure 取代旧的 migration_text：后者只认「保护向下搬家」
   346	            # 这一种形态，白银 2026-08-28「就地加固、守住 60」什么都输出不了。
   347	            out["migration"] = wall_structure(fa, pw_, spot)
   348	        except Exception as e:
   349	            print(f"⚠️ 保护迁移描述失败：{type(e).__name__}: {e}", file=sys.stderr)
   350	        # 卖方价差候选：index 只带最优腿位，明细在品种报告
   351	        try:
   352	            from undertow.analyze.credit_wall import propose as _cwp
   353	            from undertow.analyze.flow import tradeable_info as _ti_fn
   354	            _t = _ti_fn(fa)
   355	            if _t.get("side") in ("看涨", "看跌") and ga is not None:
   356	                _cw = {}
   357	                for _tier in ("conservative", "aggressive"):
   358	                    _v = _cwp(snap_curr, ref, spot, _t["side"], _t["ratio"], tier=_tier)
   359	                    if _v.ok and _v.spreads:
   360	                        _s0 = _v.spreads[0]
   361	                        _cw[_tier] = {"sell": _s0.sell_strike, "buy": _s0.buy_strike,
   362	                                      "kind": _s0.kind, "expiry": _s0.expiry.isoformat(),
   363	                                      "dte": _s0.dte, "credit": _s0.credit,
   364	                                      "occ": _s0.occupancy, "buffer": _s0.buffer_pct}
   365	                    elif not _v.ok and _tier == "conservative":
   366	                        out["credit_wall_blocked"] = _v.reason[:80]
   367	                if _cw:
   368	                    out["credit_wall"] = _cw
   369	        except Exception as e:
   370	            print(f"⚠️ 卖方价差候选失败：{type(e).__name__}: {e}", file=sys.stderr)
   371	    # 持续墙：排除 <7 天到期后的承接/压制区 —— 这才是"跌到哪有人接"的答案。
   372	    # 现行墙位会被 0DTE 劫持：2026-08-28 黄金 put 墙报 413，其 42,388 张里
   373	    # 40,394 张（95%）当天到期，收盘即归零；排除后第一大是 400（≈金价 4416），
   374	    # 那才是真正多到期分布的承接区。
   375	    try:
    72	    @property
    73	    def rate(self) -> float:
    74	        return self.hits / self.n if self.n else 0.0
    75	
    76	    @property
    77	    def significant(self) -> bool:
    78	        return self.p_value < 0.05
    79	
    80	    @property
    81	    def status(self) -> str:
    82	        if self.n == 0:
    83	            return "未验证"
    84	        if self.significant:
    85	            return "已验证"
    86	        return "样本不足"
    87	
    88	    @property
    89	    def need_more(self) -> int | None:
    90	        return samples_to_significance(self.hits, self.n)
    91	
    92	    def summary(self) -> str:
    93	        if self.kind == "corr":
    94	            # 相关型：hits 无意义，显示 r 与对照 r
    95	            s = f"n={self.n}，r={self.r:+.3f}，p={self.p_value:.3f}"
    96	            if self.r_control is not None:
    97	                s += f"（对照 r={self.r_control:+.3f}）"
    98	            if self.effect:
    99	                s += f" · {self.effect}"
   100	            return s + (" · 已验证" if self.significant else " · 未达显著")
   101	        s = f"{self.hits}/{self.n} = {self.rate:.0%}（基准 {self.baseline:.0%}），p={self.p_value:.3f}"
   102	        if self.cluster_n:
   103	            s += f"，{self.cluster_n} 个日期簇"
   104	        if self.significant:
   105	            return s + " · 已验证"
   106	        more = self.need_more
   107	        # ⚠️ need_more 假设命中率原地不变，是【乐观】下界；把抽样波动算进去
   108	        # （功效 80%）需要的样本更多 —— 强信号 65% 那条：乐观 +11，功效口径 +21。
   109	        s += (f" · 样本不足，若命中率保持不变还需 {more} 个（乐观下界；"
   110	              f"要在 80% 功效下确证需更多）"
   111	              if more else " · 命中率贴近基准，再攒样本也难以证实")
   112	        return s
   113	
   114	
   115	# ── 登记簿：每条都必须能追到一次实际回测 ────────────────────────────────────
   116	REGISTRY: dict[str, Validation] = {
   117	    "strong_signal_dir": Validation(
   118	        key="strong_signal_dir", label="强信号当日方向",
   119	        n=26, hits=17, p_value=0.169, baseline=0.50,
   120	        cluster_n=None,
   121	        note="开火信号配当日 open→close。分波动看：横盘<0.5% 时 6/12=50%（掷硬币）、"
   122	             "小动 7/10=70%、中动 3/3=100%、大动 1/1=100%。整体不显著是被横盘日稀释的。",
   123	        caveat="样本区间仅 2026-06-25 起，且横盘日占比近半"),
   124	    "tradeable_gate": Validation(
   125	        key="tradeable_gate", label="可交易信息闸门（压力比 ≥2×）",
   126	        n=62, hits=40, p_value=0.044, baseline=0.50, cluster_n=30,
   127	        note="放行组 62 笔 65%、顺向 +0.46%；拦掉组 29 笔 41%、顺向 -0.12%。Fisher p=0.044。",
   128	        caveat="共测 10 个阈值，Bonferroni 校正后 p=0.44 不再显著，待样本外验证"),
   129	    "wall_space_vote": Validation(
   130	        key="wall_space_vote", label="Gamma 墙位空间投票",
   131	        n=83, hits=41, p_value=1.000, baseline=0.51, cluster_n=43,
   132	        note="近端口径 49%、混算口径 54%、基准（无脑做多）51%。四个变体 p 全为 1.000。",
   133	        caveat="换到期口径、换阈值形式都试过，均无改善——该因子本身无预测力"),
   134	    "surface_gate": Validation(
   135	        key="surface_gate", label="固定Delta曲面闸门（逐腿与曲面矛盾即否决）",
   136	        n=66, hits=43, p_value=0.169, baseline=0.50,
   137	        note="2026-08-31 四口径对比（100 品种-日，≥2× 子集）："
   138	             "① 现状一刀切否决 65%/+0.44%　② 去掉闸门 60%/+0.31%　"
   139	             "③ 矛盾降半权 60%/+0.31%　④ 大单≥2000张豁免 64%/+0.37%。"
   140	             "放松闸门能把可判定率从 52% 提到 57%，但命中率与收益都变差 —— "
   141	             "被掐掉的不是被误伤的信号，是噪音。",
   142	        caveat="低可判定率是这套方法在诚实说「不知道」，不是待修的缺陷；"
   143	               "三种放松方案均已试过并更差，勿再改"),
   144	    "ratio_spread": Validation(
   145	        key="ratio_spread", label="比例价差检出（R15）",
   146	        kind="corr", n=4, hits=0, p_value=0.99, baseline=0.0,
   147	        r=0.0, r_control=None,
   148	        effect="信噪比（真实检出/随机基线）：纯ΔOI口径 1.0~1.5x（TQQQ 1.0x=全是巧合），"
   149	               "加当日成交量同步配对后 2.9~6.2x",
   150	        note="零假设检验：把 ΔOI 与 volume 在行权价之间随机打乱 20 次重跑。"
   151	             "GLD 2/0.7、SLV 4/1.1、QQQ 13/4.5、TQQQ 5/0.8。",
   152	        caveat="只验证了「不是随机凑对」，【未】验证检出后是否有交易价值；"
   153	               "QQQ 随机基线仍有 4.5 个，条目多时不可信"),
   154	    "expected_move": Validation(
   155	        key="expected_move", label="可判定率预告波动幅度",
   156	        kind="corr", n=66, hits=0, p_value=0.048, baseline=0.0,
   157	        r=0.243, r_control=0.053,
   158	        effect="预告的是幅度不是方向：高低两组命中率 66% vs 65%（一样），"
   159	               "当日波动 1.09% vs 0.55%（翻倍）",
   160	        note="相关性检验，非命中率检验：可判定率 vs 当日波动 r=+0.243 (t=2.01)；"
   161	             "vs 是否命中 r=+0.053（对照）。",
   162	        caveat="同批数据第二次找模式；≥80% 档仅 6 笔"),
   163	}
   164	
   165	
   166	def get(key: str) -> Validation | None:
   167	    return REGISTRY.get(key)
   168	
   169	
   170	def badge(key: str) -> str:
   171	    """给报告用的一行状态串。找不到就明说未登记，不许沉默。"""
   172	    v = REGISTRY.get(key)
   173	    if v is None:
   174	        return "⚠️ 未登记验证状态（不得作为交易依据）"
   175	    icon = {"已验证": "✅", "样本不足": "🟡", "未验证": "⚠️"}[v.status]
   176	    if v.kind == "corr":
   177	        icon = "✅" if v.significant else "🟡"
   178	    s = f"{icon} {v.status}：{v.summary()}"
   179	    if v.caveat:
   180	        s += f"　⚠️ {v.caveat}"

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "rg -n \"badge\\(|REGISTRY|get\\(\\\"expected_move|get\\(\\\"tradeable\" undertow tests; git diff a007b2d..c3b744e -- undertow/analyze/outlook.py undertow/cli.py | sed -n '1,420p'; git status --short" in /Users/yhdong/Trading
 succeeded in 0ms:
tests/test_position_alert.py:774:    from undertow.analyze.validation import (REGISTRY, badge,
tests/test_position_alert.py:777:    wall = REGISTRY["wall_space_vote"]
tests/test_position_alert.py:783:    for k, v in REGISTRY.items():
tests/test_position_alert.py:801:    assert '_val_badge("strong_signal_dir")' in banner, \
tests/test_position_alert.py:853:    from undertow.analyze.validation import REGISTRY
tests/test_position_alert.py:862:    v = REGISTRY["ratio_spread"]
undertow/report/html.py:56:def _bias_badge(o: Outlook) -> str:
undertow/report/html.py:1290:def _val_badge(key: str) -> str:
undertow/report/html.py:1294:        return badge(key)
undertow/report/html.py:1367:        f'border-radius:6px;font-size:12.5px">{_esc(_val_badge("strong_signal_dir"))}</div>'
undertow/report/html.py:1383:    from undertow.analyze.validation import REGISTRY
undertow/report/html.py:1385:    for v in REGISTRY.values():
undertow/report/html.py:1475:        f'<div style="margin:10px 0">{_bias_badge(o)}</div>'
undertow/report/html.py:2221:    ok = ti.get("tradeable")
undertow/analyze/validation.py:9:  1. 任何进入交易决策的判断，都要在 REGISTRY 里有一条，带 n / hits / p_value；
undertow/analyze/validation.py:116:REGISTRY: dict[str, Validation] = {
undertow/analyze/validation.py:167:    return REGISTRY.get(key)
undertow/analyze/validation.py:170:def badge(key: str) -> str:
undertow/analyze/validation.py:172:    v = REGISTRY.get(key)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-wpcqJZMd' (errno=Operation not permitted)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-MBL61MYF' (errno=Operation not permitted)
diff --git a/undertow/analyze/outlook.py b/undertow/analyze/outlook.py
index 147afc5..2fdcc90 100644
--- a/undertow/analyze/outlook.py
+++ b/undertow/analyze/outlook.py
@@ -3,8 +3,17 @@
 
 立场（务必牢记，已写进报告）:
   * 这是【规则化情景推演】，不是涨跌预言机。
-  * 方向 = 各因子按【回测校准的可信度】加权投票的结果——所有权重、依据、贡献都
-    显式列出、可审计；LLM 不参与算数。
+  * 方向 = 各因子加权投票的结果——所有权重、依据、贡献都显式列出、可审计；
+    LLM 不参与算数。
+  * ⚠️ 权重【不是】回测校准出来的（2026-08-31 查证）：macro 三项的权重
+    (1.5/1.0/0.6) 在 macro.py 的注册表里直接写死、无回测来源；COT 那几条的
+    注释只有定性描述（"油有效、金失效"）或没有样本量的数字；八个因子里唯一
+    有完整回测记录（n + p 值）的是 Gamma 墙位空间，而它 49%~54% 对基准 51%、
+    p=1.000，不及格。此前这里写的是"按回测校准的可信度加权"，那句话不成立，
+    会让读者以为投票结果有实证支撑 —— 用户 2026-08-31 的原话：
+    「照着做交易的时候，你又说这其实是不准的」。
+  * 每条判断的实测成绩登记在 analyze/validation.py，报告的「验证状态总览」
+    直接渲染该表，包括不及格的。
   * "关键位点"来自期权结构（墙/零伽马/资金流活跃行权价），是客观可观测的；
     "情景"是规则化的 if-then，给的是【该盯哪些位、什么情况证伪】，不是点位预言。
 """
@@ -248,11 +257,21 @@ def _gamma_vote(ga: GammaAnalysis) -> list[FactorVote]:
         down_room = (ga.spot - ga.put_wall) / ga.spot
         diff = up_room - down_room
         sign = 1 if diff > 0.02 else (-1 if diff < -0.02 else 0)
+        # ⚠️ 2026-08-31 回测：该因子在任何口径下都没有预测力。
+        #   旧 45 天混算 + 差值±2%   54%（38/70）
+        #   新 近端≤14天 + 差值±2%   49%（41/83）
+        #   新 近端≤14天 + 比值1.5   49%（37/75）
+        #   基准(无脑做多) 51%，43 个日期簇、四个变体 binomial p 全 = 1.000。
+        # 墙位口径已按用户 2026-08-31 决定改用近端（显示层确实更正确：混算会造出
+        # 实盘不存在的墙），但换口径并没有让这个因子变准 —— 它本来就不准。
+        # weight 保持 0.6 未动：降权会改变综合评分，属用户决定，不由这里擅自改。
         out.append(FactorVote(
             layer="Gamma", factor="墙位空间", direction=_DIR_CN[sign], sign=sign,
-            weight=round(0.6 * (1 if sign else 0), 2), reliability="中",
+            weight=round(0.6 * (1 if sign else 0), 2), reliability="低·未通过回测",
             detail=(f"上行至 call 墙 {ga.call_wall:.1f} 空间 {up_room*100:+.1f}%、"
-                    f"下行至 put 墙 {ga.put_wall:.1f} 空间 {down_room*100:+.1f}%。"),
+                    f"下行至 put 墙 {ga.put_wall:.1f} 空间 {down_room*100:+.1f}%（近端 ≤14天 口径）。"
+                    f"⚠️ 该因子回测命中 49%（近端）/ 54%（混算），基准 51%，"
+                    f"43 个日期簇 p=1.000 —— 与掷硬币无异，看看即可，别当依据。"),
         ))
     # Put/Call OI 比极端 → 反指（情绪过度）
     pcr = ga.put_call_ratio
diff --git a/undertow/cli.py b/undertow/cli.py
index 4c80f08..f7c3832 100644
--- a/undertow/cli.py
+++ b/undertow/cli.py
@@ -33,9 +33,13 @@ from undertow.collect.cboe_vol import CboeVolSource
 from undertow.analyze.vrp_history import assess_vrp_history, render_markdown as vrp_md
 from undertow.analyze.positioning import analyze
 from undertow.analyze.signals import generate_signals, net_bias
-from undertow.analyze.gamma import analyze_gamma, structure_delta
+from undertow.analyze.gamma import (analyze_gamma, structure_delta,
+                                   support_ladder, ladder_bands, wall_agreement)
 from undertow.analyze.flow import _live as _flow_live
-from undertow.analyze.flow import (analyze_flow, counter_signals,
+from undertow.analyze.credit_wall import propose as cw_propose
+from undertow.analyze.backmonth import scan as backmonth_scan
+from undertow.analyze.cost_gate import candidates as cost_candidates
+from undertow.analyze.flow import (analyze_flow, counter_signals, tradeable_info, detect_ratio_spreads,
                                    flip_driver_summary, structural_moves,
                                    detect_strong_signal, probe_strong_signal)
 from undertow.analyze.outlook import (build_outlook, macro_to_votes,
@@ -52,6 +56,12 @@ from undertow.report import viz
 from undertow.analyze.family import check as _family_check
 from undertow.analyze.indicators import build as _build_labels
 from undertow.report.html import (render_report_html, render_index_html,
+                          render_wall_layers_section,
+                          render_tradeable_gate,
+                          render_cost_gate,
+                          render_backmonth,
+                          render_ratio_spreads,
+                          render_credit_wall,
                           render_flow_section, render_macro_section, render_events_section,
                           render_tldr_section, render_strategy_section,
                           render_concentration_html, render_vol_regime_section,
@@ -337,6 +347,27 @@ def _flow_facts(fa, ga, ga_prev, snap_prev, snap_curr, spot: float, ref) -> dict
             out["migration"] = wall_structure(fa, pw_, spot)
         except Exception as e:
             print(f"⚠️ 保护迁移描述失败：{type(e).__name__}: {e}", file=sys.stderr)
+        # 卖方价差候选：index 只带最优腿位，明细在品种报告
+        try:
+            from undertow.analyze.credit_wall import propose as _cwp
+            from undertow.analyze.flow import tradeable_info as _ti_fn
+            _t = _ti_fn(fa)
+            if _t.get("side") in ("看涨", "看跌") and ga is not None:
+                _cw = {}
+                for _tier in ("conservative", "aggressive"):
+                    _v = _cwp(snap_curr, ref, spot, _t["side"], _t["ratio"], tier=_tier)
+                    if _v.ok and _v.spreads:
+                        _s0 = _v.spreads[0]
+                        _cw[_tier] = {"sell": _s0.sell_strike, "buy": _s0.buy_strike,
+                                      "kind": _s0.kind, "expiry": _s0.expiry.isoformat(),
+                                      "dte": _s0.dte, "credit": _s0.credit,
+                                      "occ": _s0.occupancy, "buffer": _s0.buffer_pct}
+                    elif not _v.ok and _tier == "conservative":
+                        out["credit_wall_blocked"] = _v.reason[:80]
+                if _cw:
+                    out["credit_wall"] = _cw
+        except Exception as e:
+            print(f"⚠️ 卖方价差候选失败：{type(e).__name__}: {e}", file=sys.stderr)
     # 持续墙：排除 <7 天到期后的承接/压制区 —— 这才是"跌到哪有人接"的答案。
     # 现行墙位会被 0DTE 劫持：2026-08-28 黄金 put 墙报 413，其 42,388 张里
     # 40,394 张（95%）当天到期，收盘即归零；排除后第一大是 400（≈金价 4416），
@@ -1200,7 +1231,15 @@ def cmd_report(args) -> int:
                 percentile=an.categories["managed_money"].net_percentile,
                 title="投机资金 Managed Money 净持仓历史")
 
-            flow_html = render_flow_section(fa)
+            # migration（put 墙三区域读数）从 index 移到品种报告的资金流一节
+            # （用户 2026-08-31：index 太复杂）——研究性内容不占索引页，但不消失
+            _mig = None
+            try:
+                from undertow.analyze.flow import wall_structure as _ws
+                _mig = _ws(fa, getattr(ga, "put_wall", None), curr.spot)
+            except Exception as e:
+                print(f"⚠️ {inst.key} 墙区读数失败：{type(e).__name__}: {e}", file=sys.stderr)
+            flow_html = render_flow_section(fa, migration=_mig)
             macro_html = render_macro_section(ma)
             evs = upcoming(all_events, today=today, within_days=21, instrument=inst.key)
             events_html = render_events_section(evs, today)
@@ -1355,6 +1394,90 @@ def cmd_report(args) -> int:
             strategy_html = (render_strategy_hub(strategy_props) + strategy_html
                              + render_credit_spread_section(cs_plan)
                              + render_condor_section(condor_plan))
+            # —— 可交易信息闸门（压力倍数 <2× = 今天没信息，见 flow.tradeable_info）——
+            gate_html = ""
+            try:
+                _ti = tradeable_info(fa)
+                gate_html = render_tradeable_gate(_ti, inst.display_name)
+            except Exception as e:
+                print(f"⚠️ {inst.key} 可交易闸门失败：{type(e).__name__}: {e}", file=sys.stderr)
+
+            # —— 比例价差（playbook R15）：逐腿判定会读反的结构 ——
+            ratio_html = ""
+            try:
+                _rs = detect_ratio_spreads(fa.changes, curr.spot)
+                ratio_html = render_ratio_spreads(
+                    _rs, conv=(ga.to_commodity if ratio is not None else None),
+                    etf_symbol=inst.options.symbol)
+            except Exception as e:
+                print(f"⚠️ {inst.key} 比例价差检测失败：{type(e).__name__}: {e}", file=sys.stderr)
+
+            # —— 远月结构异动（playbook R16）：近月窗口的盲区，只作长期背景 ——
+            backmonth_html = ""
+            try:
+                _bm = backmonth_scan(prev, curr, obs_day, curr.spot)
+                backmonth_html = render_backmonth(
+                    _bm, spot=curr.spot,
+                    conv=(ga.to_commodity if ratio is not None else None),
+                    etf_symbol=inst.options.symbol)
+            except Exception as e:
+                print(f"⚠️ {inst.key} 远月扫描失败：{type(e).__name__}: {e}", file=sys.stderr)
+
+            # —— 墙位卖方价差候选（analyze/credit_wall）——
+            credit_wall_html = ""
+            try:
+                if _ti and _ti.get("side") in ("看涨", "看跌"):
+                    _bp = _na = None
+                    try:
+                        from undertow.collect.longbridge_account import fetch_assets
+                        _a = fetch_assets()
+                        _bp, _na = _a.buy_power, _a.net_assets
+                    except Exception:
+                        pass
+                    _vs = {t: cw_propose(curr, obs_day, curr.spot, _ti["side"],
+                                         _ti["ratio"], tier=t)
+                           for t in ("conservative", "balanced", "aggressive")}
+                    credit_wall_html = render_credit_wall(
+                        _vs, spot=curr.spot,
+                        conv=(ga.to_commodity if ratio is not None else None),
+                        etf_symbol=inst.options.symbol, buying_power=_bp,
+                        net_assets=_na)
+            except Exception as e:
+                print(f"⚠️ {inst.key} 卖方价差失败：{type(e).__name__}: {e}", file=sys.stderr)
+
+            # —— 成本闸门：预期波动 vs 回本门槛（见 cost_gate 模块注释）——
+            cost_html = ""
+            try:
+                if _ti and _ti.get("side") in ("看涨", "看跌"):
+                    _cands = cost_candidates(curr, curr.spot, _ti["side"], obs_day,
+                                             decidable=_ti["decidable"])
+                    cost_html = render_cost_gate(
+                        _cands, spot=curr.spot, side=_ti["side"],
+                        conv=(ga.to_commodity if ratio is not None else None),
+                        etf_symbol=inst.options.symbol)
+            except Exception as e:
+                print(f"⚠️ {inst.key} 成本闸门失败：{type(e).__name__}: {e}", file=sys.stderr)
+
+            # —— 期权结构按到期分层（近端置顶）——
+            # 主报告的墙来自 analyze_gamma 的跨到期加总，会造出实盘不存在的位置；
+            # 这一节把它拆回近/中/远三层，并标出近端与中端是否指向同一位置。
+            layers_html = ""
+            try:
+                # expiring_on = 快照日 = 今天的交易日：obs_day 计时会把当日到期算进
+                # 近端支撑，但它们今晚就消失，必须在阶梯上标出来。
+                _texp = date.fromisoformat(curr_date_s) if curr_date_s else today
+                _lad = support_ladder(curr, obs_day, curr.spot, expiring_on=_texp)
+                _bands = ladder_bands(curr, obs_day, curr.spot)
+                _agree = {sd: wall_agreement(ga.layers, sd) for sd in ("put", "call")}
+                layers_html = render_wall_layers_section(
+                    ga, ladder=_lad, bands=_bands, agree=_agree,
+                    conv=(ga.to_commodity if ratio is not None else None),
+                    unit="", etf_symbol=inst.options.symbol)
+            except Exception as e:
+                # 分层失败要出声：主墙位已改用近端口径，这一节缺失会让读者
+                # 无从判断墙属于哪个到期层。
+                print(f"⚠️ {inst.key} 期权结构分层失败：{type(e).__name__}: {e}", file=sys.stderr)
+
             # —— 近周到期阶梯：逐周五/月度独立墙位+买卖方（短线定到期价差用）——
             expiry_html = ""
             try:
@@ -1487,7 +1610,12 @@ def cmd_report(args) -> int:
                                       tech_html=tech_html, stretch_read=stretch_read,
                                       indicators_html=_indicators_html,
                                       expiry_html2=_expiry_html,
-                                      summary_html=_summary_html)
+                                      summary_html=_summary_html,
+                                      layers_html=layers_html,
+                                      gate_html=gate_html, cost_html=cost_html,
+                                      backmonth_html=backmonth_html,
+                                      ratio_html=ratio_html,
+                                      credit_wall_html=credit_wall_html)
             # ⚠️ 文件名用【可交易日】（= 快照日期），不是生成日期。
             # 时点约定：快照 D 于 D 凌晨捕获，OI 是 D−1 收盘的 OCC 结算，
             # diff 描述交易日 D−1，**D 开盘才可执行** —— D 就是这份研报的身份。
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-7SVlZPMo' (errno=Operation not permitted)
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-RTyUipMm' (errno=Operation not permitted)

hook: PostToolUse
hook: PostToolUse Completed
hook: PreToolUse
hook: PreToolUse Completed
exec
/bin/zsh -lc "nl -ba undertow/report/html.py | sed -n '1280,1405p'; nl -ba tests/test_position_alert.py | sed -n '760,825p'; nl -ba tests/test_position_alert.py | sed -n '930,980p'" in /Users/yhdong/Trading
 succeeded in 0ms:
  1280	        f'<div class="sub">真正的趋势下杀应<b>同时</b>出现放量 + ATM IV 大涨 + '
  1281	        f'主翼 put 大幅变贵；❓=数据不足测不了，不算「不满足」</div>'
  1282	        f'<table><tbody>{checks}</tbody></table>'
  1283	        f'<h3>可用腿（噪音与低可靠度贡献恒为 0）</h3>{legs_html}'
  1284	        '<div class="sub" style="margin-top:8px">⚠️ 两条反直觉的判读规则：'
  1285	        '<b>①远虚 Put 加保护 ≠ 看跌到那个价位</b>，只是防那段风险，位置比数量重要；'
  1286	        '<b>②成交量大 + OI 变化大 ≠ 有方向信息</b>——多数是调仓换月。</div>'
  1287	        '</div>')
  1288	
  1289	
  1290	def _val_badge(key: str) -> str:
  1291	    """取验证状态串；模块缺失时明说，绝不静默返回空串。"""
  1292	    try:
  1293	        from undertow.analyze.validation import badge
  1294	        return badge(key)
  1295	    except Exception as e:
  1296	        return f"⚠️ 验证状态读取失败（{type(e).__name__}）——不得作为交易依据"
  1297	
  1298	
  1299	def render_strong_signal_banner(ss, display_name: str = "", stale_note: str = "") -> str:
  1300	    """近端资金流强信号置顶红/绿告警（一边倒时才由 detect_strong_signal 产出）。
  1301	
  1302	    动机：综合投票会把这种一边倒的领先信号对冲成"分歧/中性"而埋没（复盘 8/19 黄金），
  1303	    故独立置顶、显著标识。背离时额外提示"近端资金流领先、可能抢跑于慢因子"。
  1304	    """
  1305	    if ss is None:
  1306	        return ""
  1307	    up = ss.direction == "看涨"
  1308	    lowc = bool(getattr(ss, "low_confidence", False))
  1309	    # 低置信（方向裁决的软条件未过）→ 降为琥珀色"未校准异常"，不占红色置顶。
  1310	    # 检测口径不变，只是可执行性不同（codex review 2026-08-27）。
  1311	    if lowc:
  1312	        accent, bg = "#bf8700", "#fff8c5"
  1313	    else:
  1314	        accent = "#1a7f37" if up else "#cf222e"
  1315	        bg = "#e6f4ea" if up else "#ffebe9"
  1316	    arrow = "▲" if up else "▼"
  1317	    reasons = "".join(f'<li style="margin:2px 0">{_esc(r)}</li>' for r in ss.reasons)
  1318	    diverge = ""
  1319	    # 与近端一致、只和中期冲突 —— 这是最常见也最容易被误读成"自相矛盾"的情形，
  1320	    # 必须显式说出来（2026-08-27 QQQ 正是如此：近端偏空(弱)、中期偏多）。
  1321	    if not ss.diverges and getattr(ss, "conflicts_mid", False):
  1322	        diverge = (
  1323	            f'<div style="margin-top:8px;padding:8px 10px;background:#ddf4ff;'
  1324	            f'border-radius:6px;font-size:13px;color:#0a3069">'
  1325	            f'ℹ <b>与近端方向一致</b>（近端＝{_esc(ss.outlook_bias or "—")}），'
  1326	            f'<b>只与中期冲突</b>（中期＝{_esc(ss.mid_bias or "—")}）：'
  1327	            f'这不是自相矛盾，而是<b>时间尺度不同</b>——'
  1328	            f'中期看的是 COT 持仓与宏观（周频、慢），近端看的是当日期权链。'
  1329	            f'本层<b>未经回测校准</b>，不足以推翻中期结论；'
  1330	            f'合理读法是「上升背景里的短线风险窗口」。</div>'
  1331	        )
  1332	    elif ss.diverges:
  1333	        diverge = (
  1334	            f'<div style="margin-top:8px;padding:8px 10px;background:#fff8c5;'
  1335	            f'border-radius:6px;font-size:13px;color:#7d4e00">'
  1336	            f'⚠ <b>与近端方向不同向</b>（近端＝{_esc(ss.outlook_bias or "—")}）：'
  1337	            f'连当日的近端层都没站在这一侧。<b>本层未经回测校准</b>'
  1338	            f'（核心闸门需历史逐行 OI，免费源拿不到，正在用 signal_ledger 向前累积样本）——'
  1339	            f'"领先"只是一次黄金复盘得来的猜想，<b>没有统计证据</b>，'
  1340	            f'不足以据此推翻已校准的综合研判与超买超卖层。</div>'
  1341	        )
  1342	    if lowc:
  1343	        diverge = (f'<div style="margin-top:8px;padding:8px 10px;background:#fff8c5;'
  1344	                   f'border-radius:6px;font-size:13px;color:#7d4e00">'
  1345	                   f'⚠️ <b>本告警为「低置信」</b>：当日方向裁决的软条件未通过'
  1346	                   f'（压力比不足或两个口径反向），而这些阈值<b>全部未经校准</b>。'
  1347	                   f'按项目规矩，未校准的判据只记录、不正式裁决 —— '
  1348	                   f'因此本条<b>降级为观察项</b>，不作为可执行告警。</div>') + diverge
  1349	    if stale_note:
  1350	        diverge = (f'<div style="margin-top:8px;padding:8px 10px;background:#fff8c5;'
  1351	                   f'border-radius:6px;font-size:13px;color:#7d4e00">'
  1352	                   f'⚠️ <b>本告警已过期</b>：{_esc(stale_note)}'
  1353	                   f'——<b>不是今日可执行的信号</b>。</div>') + diverge
  1354	    name = f'{_esc(display_name)} · ' if display_name else ""
  1355	    return (
  1356	        f'<div class="card" style="border:2px solid {accent};background:{bg}">'
  1357	        f'<div style="font-size:20px;font-weight:800;color:{accent}">'
  1358	        f'⚡ {name}近端资金流 <span style="font-size:23px">{arrow} {_esc(ss.level)}{_esc(ss.direction)}</span></div>'
  1359	        f'<div class="sub" style="margin:4px 0 6px">期权端"一边倒"教科书组合 · '
  1360	        f'加权增仓比 {ss.pressure_ratio}× · 主翼买卖比 {ss.wing_ratio}×'
  1361	        f'{" · 波动率面追认" if ss.vol_confirms else ""}</div>'
  1362	        f'<ul style="margin:6px 0 0;padding-left:20px;font-size:13.5px">{reasons}</ul>'
  1363	        f'{diverge}'
  1364	        # 验证状态必须贴在结论旁边 —— 这个横幅是全报告最容易被当成
  1365	        # 可执行指令的地方（用户 2026-08-31：「照着做交易的时候，你又说这其实是不准的」）
  1366	        f'<div style="margin-top:8px;padding:7px 10px;background:#ffffffaa;'
  1367	        f'border-radius:6px;font-size:12.5px">{_esc(_val_badge("strong_signal_dir"))}</div>'
  1368	        f'<div class="sub" style="margin-top:8px;font-size:12px">'
  1369	        f'口径：近月主翼(20~45Δ)买卖方加权。⚠️ 它与加权增仓用同一套压力数（实测方向 100% 共线），**不是第二份独立证据**；方向裁决弃权时本告警不会出现 · 波段级情景预警，非交易指令</div>'
  1370	        f'</div>'
  1371	    )
  1372	
  1373	
  1374	def render_validation_table() -> str:
  1375	    """验证状态总览：哪些能信、哪些不能、还差多少样本才能定论。
  1376	
  1377	    起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，
  1378	    你又说这其实是不准的。现在的瓶颈到底是什么？」
  1379	    在此之前，八个投票因子里只有一个有完整回测记录（样本量+p 值），
  1380	    其余权重是拍的，而报告却用「按回测校准的可信度加权」的说法呈现。
  1381	    这张表把每条判断的实际成绩摊开，包括不及格的。
  1382	    """
  1383	    from undertow.analyze.validation import REGISTRY
  1384	    rows = []
  1385	    for v in REGISTRY.values():
  1386	        if v.kind == "corr":
  1387	            icon = "✅" if v.significant else "🟡"
  1388	            score = f"r={v.r:+.3f}"
  1389	            extra = f"对照 r={v.r_control:+.3f}" if v.r_control is not None else ""
  1390	        else:
  1391	            icon = {"已验证": "✅", "样本不足": "🟡", "未验证": "⚠️"}[v.status]
  1392	            score = f"{v.hits}/{v.n} = {v.rate:.0%}"
  1393	            more = v.need_more
  1394	            extra = ("已达显著" if v.significant else
  1395	                     (f"还差 {more} 个样本" if more else "命中率贴近基准，难以证实"))
  1396	        color = "#1a7f37" if v.significant else "#bc4c00"
  1397	        rows.append(
  1398	            f'<tr><td>{icon} {_esc(v.label)}</td>'
  1399	            f'<td class="r">{score}</td>'
  1400	            f'<td class="r">{v.p_value:.3f}</td>'
  1401	            f'<td style="color:{color}">{_esc(extra)}</td>'
  1402	            f'<td class="sub">{_esc(v.caveat)}</td></tr>')
  1403	    return (
  1404	        '<div class="card"><h2>验证状态总览 · 哪些能信，哪些不能</h2>'
  1405	        '<div class="sub">每条会影响交易决策的判断，都要在这里登记实测成绩。'
   760	    fn = src[src.index("def render_cost_gate"):]
   761	    assert "多重比较" in fn, "多重比较风险必须写在报告里，不得只留在代码注释"
   762	    assert "不解决方向" in fn, "必须说明这张表只解决幅度"
   763	    print("PASS test_cost_gate_rejects_the_actual_losing_trade")
   764	
   765	
   766	def test_validation_registry_is_honest():
   767	    """验证登记簿必须诚实：不及格的也要在，且报告必须展示 n 与 p。
   768	
   769	    起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，
   770	    你又说这其实是不准的。现在的瓶颈到底是什么？」
   771	    查下来：八个投票因子里只有一个有完整回测记录，其余权重是拍的，
   772	    而 outlook.py 开头却写着「按回测校准的可信度加权」。
   773	    """
   774	    from undertow.analyze.validation import (REGISTRY, badge,
   775	                                             samples_to_significance, binom_p)
   776	    # ① 不及格的必须留在表里，不许因为难看就删掉
   777	    wall = REGISTRY["wall_space_vote"]
   778	    assert wall.p_value >= 0.05 and wall.rate <= wall.baseline, \
   779	        "墙位空间因子的不及格结果必须保留"
   780	    assert "无预测力" in wall.caveat
   781	
   782	    # ② 每条都要有样本量；相关型不得被当成命中率显示
   783	    for k, v in REGISTRY.items():
   784	        assert v.n > 0, f"{k} 缺样本量"
   785	        assert v.kind in ("hit", "corr")
   786	        if v.kind == "corr":
   787	            assert v.r is not None, f"{k} 是相关型但没记 r"
   788	            assert "%" not in v.summary().split("·")[0] or "r=" in v.summary(), \
   789	                f"{k} 相关型不得显示成命中率"
   790	
   791	    # ③ 「还差多少样本」必须真的能算，且贴近基准时明确返回 None
   792	    assert samples_to_significance(17, 26) == 11
   793	    assert samples_to_significance(41, 83) is None, "命中率低于基准时不该给希望"
   794	    assert binom_p(17, 26) > 0.05 and binom_p(40, 62) < 0.05
   795	
   796	    # ④ 报告必须把验证状态贴在强信号横幅上——那是最容易被当指令的地方
   797	    src = (Path(__file__).resolve().parents[1] / "undertow" / "report"
   798	           / "html.py").read_text("utf-8")
   799	    banner = src[src.index("def render_strong_signal_banner"):
   800	                 src.index("def render_validation_table")]
   801	    assert '_val_badge("strong_signal_dir")' in banner, \
   802	        "强信号横幅必须显示该信号的实测成绩"
   803	    # ⑤ 读取失败时必须出声，不得静默返回空串
   804	    helper = src[src.index("def _val_badge"):src.index("def render_strong_signal_banner")]
   805	    assert "不得作为交易依据" in helper and "return \"\"" not in helper
   806	    # ⑥ 总览必须说明「已验证」不等于可以照着下单
   807	    tbl = src[src.index("def render_validation_table"):]
   808	    tbl = tbl[:tbl.index("\n\ndef ") if "\n\ndef " in tbl else len(tbl)]
   809	    assert "Bonferroni" in tbl and "不等于可以照着下单" in tbl
   810	    print("PASS test_validation_registry_is_honest")
   811	
   812	
   813	def test_backmonth_scale_isolation():
   814	    """远月结构异动必须与日度方向研判隔离（playbook R16 的时间尺度纪律）。
   815	
   816	    机构可以一边持有远月上行尾部、一边在近月做空 —— 2026-08-31 白银正是如此：
   817	    近月「⚡极强看跌 9.5×」，同日远月 call 新增 29,875 张 vs put 5,873。
   818	    若把远月并入近月压力，方向直接被污染。
   819	    """
   820	    from undertow.analyze import backmonth as bm
   821	    src_bm = (Path(__file__).resolve().parents[1] / "undertow" / "analyze"
   822	              / "backmonth.py").read_text("utf-8")
   823	    assert "不进综合分" in src_bm and "不改任何近月位点" in src_bm
   824	
   825	    # 扫描器不得被 flow/outlook 引用——一旦被引用就等于进了方向计算
   930	
   931	def test_sizing_is_kelly_not_fixed_pct():
   932	    """仓位按 Kelly 判，不按净资产固定百分比。
   933	
   934	    用户 2026-08-31：「原本我们的守则是仓位管理风险 10%？但是为了这个 10%，
   935	    却可能放弃更优的交易，而选择次优，这反而放大了风险。风险管理得再好，
   936	    永远在亏损有啥用呢。」
   937	
   938	    这不是偏好问题是算术：净资产 $264 × 10% = $26，买不起任何一张价差
   939	    （最小占用 $86）。固定百分比对小账户 = 强制选择负期望的廉价合约。
   940	
   941	    锁三件事：
   942	      ① 负优势时必须拒绝（仓位再小也是慢慢亏）
   943	      ② 1 组超过 Kelly 时不得静默压到 0 —— 那等于"不能交易"，
   944	         要把超配倍数摆出来交给人决定
   945	      ③ 盈亏比（b）必须真的按 赢/亏 算，且实测里激进档的 b 高于稳健档
   946	    """
   947	    from undertow.analyze.sizing import kelly, size, ruin_probability
   948	    from undertow.analyze.credit_wall import RISK_TIERS
   949	
   950	    # ③ 关键反直觉事实：激进档胜率低但盈亏比远高（买腿保护近，破墙也只破一点）
   951	    cons = RISK_TIERS["conservative"]
   952	    aggr = RISK_TIERS["aggressive"]
   953	    kc = kelly(cons["win_rate"], cons["per_trade_pct"], cons["win_roi_pct"])
   954	    ka = kelly(aggr["win_rate"], aggr["per_trade_pct"], aggr["win_roi_pct"])
   955	    assert aggr["win_rate"] < cons["win_rate"], "激进档胜率更低"
   956	    assert ka.odds > kc.odds * 3, "但激进档盈亏比必须显著更高，这是它能上仓位的原因"
   957	    assert ka.kelly > kc.kelly, "Kelly 应据此给激进档更大仓位"
   958	    assert kc.lose_roi > ka.lose_roi, "稳健档输的时候亏得更多（卖得远=被打穿即深实值）"
   959	
   960	    # ① 负优势拒绝
   961	    bad = kelly(0.30, -5.0, 10.0)
   962	    assert not bad.positive_edge
   963	    v = size(264.0, 100.0, bad)
   964	    assert not v.ok and "不该做" in v.reason
   965	
   966	    # ② 1 组超 Kelly 不得静默归零
   967	    v2 = size(264.0, 96.0, kc, buying_power=153.0)
   968	    assert v2.ok and v2.n_units == 1, "不得因超 Kelly 就判成不能做"
   969	    assert v2.over_kelly > 1.0
   970	    assert "不存在" in v2.reason, "必须说明「压到 Kelly 以下的选项不存在」"
   971	
   972	    # 买不起要明说
   973	    v3 = size(264.0, 900.0, ka, buying_power=153.0)
   974	    assert not v3.ok and "做不了" in v3.reason
   975	
   976	    # 破产概率随胜率下降而升高
   977	    assert ruin_probability(0.23, 3) > ruin_probability(0.82, 3) * 10
   978	
   979	    src = (Path(__file__).resolve().parents[1] / "undertow" / "analyze"
   980	           / "sizing.py").read_text("utf-8")

hook: PostToolUse
hook: PostToolUse Completed
codex
结论：这 9 个提交目前不应进入实盘决策链。最危险的不是单个公式，而是“未按日期簇验证的收益 → Kelly 50% → 即使超过 Kelly 仍建议 1 组 → 鼓励持有到期”的串联放大。

## P0：可能直接造成错误仓位或账户级损失

1. `size()` 明知超过 Kelly，仍返回 `ok=True` 和 `n_units=1`

位置：[sizing.py:87](/Users/yhdong/Trading/undertow/analyze/sizing.py:87)、[sizing.py:116](/Users/yhdong/Trading/undertow/analyze/sizing.py:116)、[test_position_alert.py:966](/Users/yhdong/Trading/tests/test_position_alert.py:966)

`max_over_kelly` 只改变“可接受/明显超配”文字，不构成限制。即使 1 组是 Kelly 的 6 倍、10 倍，只要购买力够，仍返回可下单。测试甚至把这一行为锁死。

不可分割不代表应该向上取整；正确处理应是“最小单位超过允许风险 → `ok=False`，不交易”。当前实现本质上是鼓励超配，直接违反 Kelly 本身。

2. 报告中的“破产概率上限”不是破产概率，更不是上限

位置：[sizing.py:125](/Users/yhdong/Trading/undertow/analyze/sizing.py:125)、[html.py:2447](/Users/yhdong/Trading/undertow/report/html.py:2447)

```python
(1 - win_rate) ** n_full_loss_to_ruin
```

只是在独立同分布假设下，“下一段恰好连续 n 次全损”的概率。真实破产可以由非连续亏损、部分亏损、仓位变化、相关信号及费用共同造成，因此这个数通常严重低估破产风险。把它标成“上限”会给用户错误安全感。

3. 候选权利金来自不可成交的盘前旧盘口，且 DTE 整体错一天

位置：[cli.py:1140](/Users/yhdong/Trading/undertow/cli.py:1140)、[cli.py:1445](/Users/yhdong/Trading/undertow/cli.py:1445)、[credit_wall.py:232](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:232)、[credit_wall.py:277](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:277)、[cost_gate.py:174](/Users/yhdong/Trading/undertow/analyze/cost_gate.py:174)

实际检查 `GLD/2026-08-31` 快照：

- 保存时间约为 ET 06:00；
- payload 时间约为 ET 05:42；
- 合约最后成交仍是 8 月 28 日；
- 普通 ETF 期权当时尚未进入 09:30 的核心交易时段。Cboe 公布的普通美股期权核心交易时段从 09:30 ET 开始。[Cboe 交易时段](https://www.cboe.com/about/hours/us-options/)

所以 `sell.bid - buy.ask` 并不是周一开盘可实现的成交价，而主要是上周五遗留/盘前指示报价。周末跳空、开盘 IV 和点差变化全部漏掉。

同时，实际可执行日是快照日 D，代码却将 `obs_day=D−1` 传入候选生成。结果是实际 0DTE 被当成 1DTE，所有风险档窗口整体偏移一天。

4. “持有到期更好”被当成实盘建议，但回测没有模拟提前指派、到期强平和 pin risk

位置：[credit_wall.py:62](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:62)、[html.py:2519](/Users/yhdong/Trading/undertow/report/html.py:2519)、[html.py:2530](/Users/yhdong/Trading/undertow/report/html.py:2530)

美式 ETF 期权的卖方在到期前任何时候都可能被指派；深度实值、除息前和借券困难都会提高概率。OCC 明确说明，美式期权卖方从卖出至到期期间始终面临指派，并可能延迟收到通知。[OCC ODD](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)

理论到期 payoff 仍可用于模型，但对 $264 账户不等于可实现收益：

- 短 put 指派可能要求买入 100 股；
- 买腿未实值时不能完全同步保护；
- 券商可能在到期前自行平仓，成交价不由用户控制；
- 到期附近落在两腿之间会产生隔夜股票敞口。

[html.py:2531](/Users/yhdong/Trading/undertow/report/html.py:2531) 对“长桥保证金不足即强平、不会负债”的断言没有代码内证据，而且“强平”正意味着回测的到期 payoff 无法实现。

5. 成本闸门用未验证的小样本均值输出绿色“可做”结论

位置：[cost_gate.py:42](/Users/yhdong/Trading/undertow/analyze/cost_gate.py:42)、[cost_gate.py:135](/Users/yhdong/Trading/undertow/analyze/cost_gate.py:135)、[html.py:2271](/Users/yhdong/Trading/undertow/report/html.py:2271)、[html.py:2289](/Users/yhdong/Trading/undertow/report/html.py:2289)

即使最高档只有 6 笔，`weak` 也只加一行文字，不会阻止：

```python
ok = expected_mean_move > estimated_breakeven
```

随后报告仍显示绿色 ✅ 和“典型幅度够覆盖成本”。

此外：

- 条件均值大于门槛，不等于盈利概率大于 50%，更不等于期望 P&L 为正；
- `Δ + θ` 是局部一阶近似，忽略 gamma、IV 变化和未来点差，近到期合约恰恰最不适合该近似；
- 模型比较的是绝对波动均值和“顺向所需波动”，没有纳入方向命中概率。

因此它最多是研究性量级提示，不能是硬“做/不做”闸门。

## P1：统计、口径或验证结论错误

6. 60 笔不是 60 个独立样本；当前 p 值、Kelly 输入均失真

位置：[credit_wall.py:56](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:56)、[credit_wall.py:68](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:68)、[credit_wall.py:80](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:80)、[credit_wall.py:93](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:93)

同一信号下的多个到期共享：

- 同一个方向信号；
- 同一标的路径；
- 同一事件和波动冲击；
- 大量重叠持有期。

正确统计单位应是：

1. 先明确一个事前可执行的“每信号只选一个到期”规则；或把同日全部候选合成一个固定权重的组合收益；
2. 再按日期聚合跨品种；
3. 对日期簇做 block bootstrap/permutation，报告日期簇数和收益置信区间。

所以 38/45/60 不能直接作为独立 n。有效 n 至多是独立信号日期数，而且很可能明显小于 45。

7. “提高同到期门槛至样本量相近”不能排除幸存者偏差

位置：[credit_wall.py:9](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:9)、[credit_wall.py:19](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:19)

改变阈值只是在同一批数据上重新筛选样本，无法处理：

- 多重比较和参数选择偏差；
- 两组信号日期、品种、到期分布不匹配；
- 同一日期重复候选；
- “加总墙”与“同到期墙”条件集合不同。

要证明加总墙优于同到期墙，至少需要同一信号、同一到期、同一腿位下的配对比较，再按日期簇检验，最好留出样本外区间。当前“所以这不是幸存者偏差”的结论不成立。

8. 胜率的二项 p 值不能验证卖方策略有正期望

位置：[credit_wall.py:23](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:23)、[credit_wall.py:93](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:93)

卖方策略可以 80% 胜率仍然亏钱，代码自己的 balanced 档就是 76% 胜率、单笔 −0.43%。因此检验“胜率是否高于 50%”答错了问题。

应检验日期簇组合的净收益是否高于零，并同时报告：

- cluster n；
- 均值/中位数；
- 尾部亏损；
- bootstrap 置信区间或簇置换 p 值。

`p_value=0.000` 也不应如此显示，且没有可复现计算来源。

9. Kelly 把连续、偏态、尾部型收益强行压成二项分布

位置：[sizing.py:56](/Users/yhdong/Trading/undertow/analyze/sizing.py:56)、[sizing.py:66](/Users/yhdong/Trading/undertow/analyze/sizing.py:66)、[credit_wall.py:68](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:68)

用平均盈利和反解的平均亏损套二项 Kelly，只在每次收益确实只有两个固定结果时成立。这里同时存在小赢、小亏、全损、指派和强平尾部，正确 Kelly 应直接对逐笔收益分布求解最大化：

\[
\sum_i \log(1+f r_i)
\]

还应按日期簇重采样，并对参数不确定性做折扣。

尤其 aggressive 档一边写平均亏损约 7.1%，一边写最差 −105%；用前者算出的 50% Kelly 几乎完全忽略了真正决定账户生存的尾部。

`abs()` 还会把不可能的输入组合静默修成合法亏损率；应校验 `E = pW-(1-p)L` 能否产生正的 `L`，否则拒绝。

10. 候选按当日年化 ROI 排序，但回测没有验证这个选择规则

位置：[credit_wall.py:297](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:297)、[html.py:2445](/Users/yhdong/Trading/undertow/report/html.py:2445)

代码将所有候选按 `annual_roi` 排序，然后报告/Kelly 使用 `spreads[0]`。但档位统计是多个到期候选的总体均值，不是“每天挑年化最高者”的回测。

这属于策略定义漂移：总体 82% 胜率和 +2.84% 不能直接赋给当前排名第一的候选。必须按完全相同的排序规则重跑“一信号一笔”回测。

11. 2× 闸门的 Fisher p=0.044 没有按日期簇计算

位置：[flow.py:2094](/Users/yhdong/Trading/undertow/analyze/flow.py:2094)、[flow.py:2115](/Users/yhdong/Trading/undertow/analyze/flow.py:2115)、[validation.py:124](/Users/yhdong/Trading/undertow/analyze/validation.py:124)

登记簿虽然写了 `cluster_n=30/36`，但 Fisher 检验仍把 91 个品种日当独立观察。打印日期簇数不等于使用日期簇推断。

而且 Bonferroni 后 p=0.44。把它设成 `TRADEABLE_MIN_RATIO=2.0` 并输出“今天没有信息”，明显早于证据。

更微妙的是，它实际上没有真正压制后面的综合研判或强信号，只是在报告顶部增加相反横幅：[html.py:2211](/Users/yhdong/Trading/undertow/report/html.py:2211)。因此当前既可能错误拦截，又会留下“⛔无信息”和“⚡极强方向”同时出现。

12. 验证登记簿会把未经簇修正、未经多重比较修正的结果标成“已验证”

位置：[validation.py:76](/Users/yhdong/Trading/undertow/analyze/validation.py:76)、[validation.py:81](/Users/yhdong/Trading/undertow/analyze/validation.py:81)、[validation.py:154](/Users/yhdong/Trading/undertow/analyze/validation.py:154)、[html.py:1383](/Users/yhdong/Trading/undertow/report/html.py:1383)

`status` 只检查原始 `p_value < 0.05`。因此 expected move 的 raw p=0.048 会显示绿色“已验证”，尽管：

- 是同批数据第二次找模式；
- 未记录/使用日期簇；
- 极端档 n=6；
- 没有样本外表现。

更严重的是，真正进入仓位的 `credit_wall` 和 Kelly 根本未登记在 `REGISTRY`，违反该模块自己声明的铁律。

13. 提前平仓公式方向正确，但“提前平仓更差”仍未被证明

位置：[credit_wall.py:62](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:62)、[html.py:2519](/Users/yhdong/Trading/undertow/report/html.py:2519)

平仓 debit 使用“买回短腿 ask − 卖出长腿 bid”是正确、保守且可成交的口径，不应改成 mid 来迎合业界结论。

问题在模拟数据和采样：

- 每日一次快照会漏掉盘中达到 50%/70% 止盈后反弹的情况；
- 快照是在非交易时段取得，盘口可能陈旧；
- 未模拟指派及券商到期前强平；
- 没有按信号日期簇比较；
- 回测实现没有留在仓库，无法复核目标触发、费用和缺失报价处理。

所以“这批每日快照模拟里提前退出较差”可以保留；“因此实盘应持有到期”不能锁死。

14. `px_on()` 取到期日之后的最近收盘属于明确的 look-ahead

用户提到的 `px_on()` 不存在于当前仓库，因此没有可给出的文件行号；这本身说明关键回测不可复现。

如果到期日价格缺失，取之后最近一天会将到期后的价格变动带入结算，改变内在价值。正确处理是：

- 只接受合约实际最后交易日/到期日对应的标的收盘或官方结算价；
- 缺失则丢弃该笔并报告缺失数；
- 不能向后找价格。

## P2：应改进但不是首要止损项

15. `layered_walls` 的 `obs_day` 只适合解释 OI 所属交易日，不适合生成今日交易候选

位置：[gamma.py:505](/Users/yhdong/Trading/undertow/analyze/gamma.py:505)、[cli.py:1140](/Users/yhdong/Trading/undertow/cli.py:1140)

对“这份 OI 描述 D−1”而言使用 `obs_day` 是一致的；对“在 D 开盘下什么到期”而言必须使用 D。建议明确拆成两个参数：

- `oi_session_date`：解释结构；
- `execution_date`：筛选可交易合约、计算 DTE 和成本。

现在一个 `today` 同时承担两种语义，已经造成错一天。

16. 回测代码和逐笔账本没有入库

目前只有硬编码汇总值和注释，没有：

- 每笔 signal date、entry quote timestamp、expiry、腿位；
- 实际结算价与来源；
- 被排除样本；
- 日期簇标识；
- 参数网格及完整结果；
- 提前退出逐日路径。

在用户会据此实盘的项目里，这些结果不能仅存在于一次临时分析会话中。

## 对 A–F 的直接判断

- A1：不能排除幸存者偏差；提高门槛到相似 n 不成立。
- A2：卖 bid/买 ask 本身保守，但这里的盘前盘口不可成交且跨周末陈旧，回测入口价无效。
- A3：遗漏对理论终值影响有限，对 $264 账户的可实现路径影响非常严重。
- A4：必须按日期簇；同一信号的多个到期先合成一笔，不能分别算独立样本。
- B：平仓成本算法正确；“提前平仓必然更差”的实盘结论没有被当前模拟支持。
- C1/C2：平均二项 Kelly 不适合该尾部分布和小样本。
- C3：是，`ok=True` 等于鼓励超配；应该返回拒绝。
- D：是，Bonferroni p=0.44 且 Fisher 未聚类，硬闸门过早。
- E：是，n=6 的点估计直接参与绿色硬判定，远超证据强度。
- F：`obs_day` 用于解释结构可以，用于交易 DTE 不可以；`px_on()` 向后取价是前瞻；EXPECTED_MOVE_TABLE 回用于全样本属于 in-sample 污染。

我没有修改代码。测试也未能运行：当前环境的系统 Python 没有安装 `pytest`；静态检查和快照核对不受影响。
hook: Stop
hook: Stop Completed
tokens used
131,347
结论：这 9 个提交目前不应进入实盘决策链。最危险的不是单个公式，而是“未按日期簇验证的收益 → Kelly 50% → 即使超过 Kelly 仍建议 1 组 → 鼓励持有到期”的串联放大。

## P0：可能直接造成错误仓位或账户级损失

1. `size()` 明知超过 Kelly，仍返回 `ok=True` 和 `n_units=1`

位置：[sizing.py:87](/Users/yhdong/Trading/undertow/analyze/sizing.py:87)、[sizing.py:116](/Users/yhdong/Trading/undertow/analyze/sizing.py:116)、[test_position_alert.py:966](/Users/yhdong/Trading/tests/test_position_alert.py:966)

`max_over_kelly` 只改变“可接受/明显超配”文字，不构成限制。即使 1 组是 Kelly 的 6 倍、10 倍，只要购买力够，仍返回可下单。测试甚至把这一行为锁死。

不可分割不代表应该向上取整；正确处理应是“最小单位超过允许风险 → `ok=False`，不交易”。当前实现本质上是鼓励超配，直接违反 Kelly 本身。

2. 报告中的“破产概率上限”不是破产概率，更不是上限

位置：[sizing.py:125](/Users/yhdong/Trading/undertow/analyze/sizing.py:125)、[html.py:2447](/Users/yhdong/Trading/undertow/report/html.py:2447)

```python
(1 - win_rate) ** n_full_loss_to_ruin
```

只是在独立同分布假设下，“下一段恰好连续 n 次全损”的概率。真实破产可以由非连续亏损、部分亏损、仓位变化、相关信号及费用共同造成，因此这个数通常严重低估破产风险。把它标成“上限”会给用户错误安全感。

3. 候选权利金来自不可成交的盘前旧盘口，且 DTE 整体错一天

位置：[cli.py:1140](/Users/yhdong/Trading/undertow/cli.py:1140)、[cli.py:1445](/Users/yhdong/Trading/undertow/cli.py:1445)、[credit_wall.py:232](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:232)、[credit_wall.py:277](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:277)、[cost_gate.py:174](/Users/yhdong/Trading/undertow/analyze/cost_gate.py:174)

实际检查 `GLD/2026-08-31` 快照：

- 保存时间约为 ET 06:00；
- payload 时间约为 ET 05:42；
- 合约最后成交仍是 8 月 28 日；
- 普通 ETF 期权当时尚未进入 09:30 的核心交易时段。Cboe 公布的普通美股期权核心交易时段从 09:30 ET 开始。[Cboe 交易时段](https://www.cboe.com/about/hours/us-options/)

所以 `sell.bid - buy.ask` 并不是周一开盘可实现的成交价，而主要是上周五遗留/盘前指示报价。周末跳空、开盘 IV 和点差变化全部漏掉。

同时，实际可执行日是快照日 D，代码却将 `obs_day=D−1` 传入候选生成。结果是实际 0DTE 被当成 1DTE，所有风险档窗口整体偏移一天。

4. “持有到期更好”被当成实盘建议，但回测没有模拟提前指派、到期强平和 pin risk

位置：[credit_wall.py:62](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:62)、[html.py:2519](/Users/yhdong/Trading/undertow/report/html.py:2519)、[html.py:2530](/Users/yhdong/Trading/undertow/report/html.py:2530)

美式 ETF 期权的卖方在到期前任何时候都可能被指派；深度实值、除息前和借券困难都会提高概率。OCC 明确说明，美式期权卖方从卖出至到期期间始终面临指派，并可能延迟收到通知。[OCC ODD](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)

理论到期 payoff 仍可用于模型，但对 $264 账户不等于可实现收益：

- 短 put 指派可能要求买入 100 股；
- 买腿未实值时不能完全同步保护；
- 券商可能在到期前自行平仓，成交价不由用户控制；
- 到期附近落在两腿之间会产生隔夜股票敞口。

[html.py:2531](/Users/yhdong/Trading/undertow/report/html.py:2531) 对“长桥保证金不足即强平、不会负债”的断言没有代码内证据，而且“强平”正意味着回测的到期 payoff 无法实现。

5. 成本闸门用未验证的小样本均值输出绿色“可做”结论

位置：[cost_gate.py:42](/Users/yhdong/Trading/undertow/analyze/cost_gate.py:42)、[cost_gate.py:135](/Users/yhdong/Trading/undertow/analyze/cost_gate.py:135)、[html.py:2271](/Users/yhdong/Trading/undertow/report/html.py:2271)、[html.py:2289](/Users/yhdong/Trading/undertow/report/html.py:2289)

即使最高档只有 6 笔，`weak` 也只加一行文字，不会阻止：

```python
ok = expected_mean_move > estimated_breakeven
```

随后报告仍显示绿色 ✅ 和“典型幅度够覆盖成本”。

此外：

- 条件均值大于门槛，不等于盈利概率大于 50%，更不等于期望 P&L 为正；
- `Δ + θ` 是局部一阶近似，忽略 gamma、IV 变化和未来点差，近到期合约恰恰最不适合该近似；
- 模型比较的是绝对波动均值和“顺向所需波动”，没有纳入方向命中概率。

因此它最多是研究性量级提示，不能是硬“做/不做”闸门。

## P1：统计、口径或验证结论错误

6. 60 笔不是 60 个独立样本；当前 p 值、Kelly 输入均失真

位置：[credit_wall.py:56](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:56)、[credit_wall.py:68](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:68)、[credit_wall.py:80](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:80)、[credit_wall.py:93](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:93)

同一信号下的多个到期共享：

- 同一个方向信号；
- 同一标的路径；
- 同一事件和波动冲击；
- 大量重叠持有期。

正确统计单位应是：

1. 先明确一个事前可执行的“每信号只选一个到期”规则；或把同日全部候选合成一个固定权重的组合收益；
2. 再按日期聚合跨品种；
3. 对日期簇做 block bootstrap/permutation，报告日期簇数和收益置信区间。

所以 38/45/60 不能直接作为独立 n。有效 n 至多是独立信号日期数，而且很可能明显小于 45。

7. “提高同到期门槛至样本量相近”不能排除幸存者偏差

位置：[credit_wall.py:9](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:9)、[credit_wall.py:19](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:19)

改变阈值只是在同一批数据上重新筛选样本，无法处理：

- 多重比较和参数选择偏差；
- 两组信号日期、品种、到期分布不匹配；
- 同一日期重复候选；
- “加总墙”与“同到期墙”条件集合不同。

要证明加总墙优于同到期墙，至少需要同一信号、同一到期、同一腿位下的配对比较，再按日期簇检验，最好留出样本外区间。当前“所以这不是幸存者偏差”的结论不成立。

8. 胜率的二项 p 值不能验证卖方策略有正期望

位置：[credit_wall.py:23](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:23)、[credit_wall.py:93](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:93)

卖方策略可以 80% 胜率仍然亏钱，代码自己的 balanced 档就是 76% 胜率、单笔 −0.43%。因此检验“胜率是否高于 50%”答错了问题。

应检验日期簇组合的净收益是否高于零，并同时报告：

- cluster n；
- 均值/中位数；
- 尾部亏损；
- bootstrap 置信区间或簇置换 p 值。

`p_value=0.000` 也不应如此显示，且没有可复现计算来源。

9. Kelly 把连续、偏态、尾部型收益强行压成二项分布

位置：[sizing.py:56](/Users/yhdong/Trading/undertow/analyze/sizing.py:56)、[sizing.py:66](/Users/yhdong/Trading/undertow/analyze/sizing.py:66)、[credit_wall.py:68](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:68)

用平均盈利和反解的平均亏损套二项 Kelly，只在每次收益确实只有两个固定结果时成立。这里同时存在小赢、小亏、全损、指派和强平尾部，正确 Kelly 应直接对逐笔收益分布求解最大化：

\[
\sum_i \log(1+f r_i)
\]

还应按日期簇重采样，并对参数不确定性做折扣。

尤其 aggressive 档一边写平均亏损约 7.1%，一边写最差 −105%；用前者算出的 50% Kelly 几乎完全忽略了真正决定账户生存的尾部。

`abs()` 还会把不可能的输入组合静默修成合法亏损率；应校验 `E = pW-(1-p)L` 能否产生正的 `L`，否则拒绝。

10. 候选按当日年化 ROI 排序，但回测没有验证这个选择规则

位置：[credit_wall.py:297](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:297)、[html.py:2445](/Users/yhdong/Trading/undertow/report/html.py:2445)

代码将所有候选按 `annual_roi` 排序，然后报告/Kelly 使用 `spreads[0]`。但档位统计是多个到期候选的总体均值，不是“每天挑年化最高者”的回测。

这属于策略定义漂移：总体 82% 胜率和 +2.84% 不能直接赋给当前排名第一的候选。必须按完全相同的排序规则重跑“一信号一笔”回测。

11. 2× 闸门的 Fisher p=0.044 没有按日期簇计算

位置：[flow.py:2094](/Users/yhdong/Trading/undertow/analyze/flow.py:2094)、[flow.py:2115](/Users/yhdong/Trading/undertow/analyze/flow.py:2115)、[validation.py:124](/Users/yhdong/Trading/undertow/analyze/validation.py:124)

登记簿虽然写了 `cluster_n=30/36`，但 Fisher 检验仍把 91 个品种日当独立观察。打印日期簇数不等于使用日期簇推断。

而且 Bonferroni 后 p=0.44。把它设成 `TRADEABLE_MIN_RATIO=2.0` 并输出“今天没有信息”，明显早于证据。

更微妙的是，它实际上没有真正压制后面的综合研判或强信号，只是在报告顶部增加相反横幅：[html.py:2211](/Users/yhdong/Trading/undertow/report/html.py:2211)。因此当前既可能错误拦截，又会留下“⛔无信息”和“⚡极强方向”同时出现。

12. 验证登记簿会把未经簇修正、未经多重比较修正的结果标成“已验证”

位置：[validation.py:76](/Users/yhdong/Trading/undertow/analyze/validation.py:76)、[validation.py:81](/Users/yhdong/Trading/undertow/analyze/validation.py:81)、[validation.py:154](/Users/yhdong/Trading/undertow/analyze/validation.py:154)、[html.py:1383](/Users/yhdong/Trading/undertow/report/html.py:1383)

`status` 只检查原始 `p_value < 0.05`。因此 expected move 的 raw p=0.048 会显示绿色“已验证”，尽管：

- 是同批数据第二次找模式；
- 未记录/使用日期簇；
- 极端档 n=6；
- 没有样本外表现。

更严重的是，真正进入仓位的 `credit_wall` 和 Kelly 根本未登记在 `REGISTRY`，违反该模块自己声明的铁律。

13. 提前平仓公式方向正确，但“提前平仓更差”仍未被证明

位置：[credit_wall.py:62](/Users/yhdong/Trading/undertow/analyze/credit_wall.py:62)、[html.py:2519](/Users/yhdong/Trading/undertow/report/html.py:2519)

平仓 debit 使用“买回短腿 ask − 卖出长腿 bid”是正确、保守且可成交的口径，不应改成 mid 来迎合业界结论。

问题在模拟数据和采样：

- 每日一次快照会漏掉盘中达到 50%/70% 止盈后反弹的情况；
- 快照是在非交易时段取得，盘口可能陈旧；
- 未模拟指派及券商到期前强平；
- 没有按信号日期簇比较；
- 回测实现没有留在仓库，无法复核目标触发、费用和缺失报价处理。

所以“这批每日快照模拟里提前退出较差”可以保留；“因此实盘应持有到期”不能锁死。

14. `px_on()` 取到期日之后的最近收盘属于明确的 look-ahead

用户提到的 `px_on()` 不存在于当前仓库，因此没有可给出的文件行号；这本身说明关键回测不可复现。

如果到期日价格缺失，取之后最近一天会将到期后的价格变动带入结算，改变内在价值。正确处理是：

- 只接受合约实际最后交易日/到期日对应的标的收盘或官方结算价；
- 缺失则丢弃该笔并报告缺失数；
- 不能向后找价格。

## P2：应改进但不是首要止损项

15. `layered_walls` 的 `obs_day` 只适合解释 OI 所属交易日，不适合生成今日交易候选

位置：[gamma.py:505](/Users/yhdong/Trading/undertow/analyze/gamma.py:505)、[cli.py:1140](/Users/yhdong/Trading/undertow/cli.py:1140)

对“这份 OI 描述 D−1”而言使用 `obs_day` 是一致的；对“在 D 开盘下什么到期”而言必须使用 D。建议明确拆成两个参数：

- `oi_session_date`：解释结构；
- `execution_date`：筛选可交易合约、计算 DTE 和成本。

现在一个 `today` 同时承担两种语义，已经造成错一天。

16. 回测代码和逐笔账本没有入库

目前只有硬编码汇总值和注释，没有：

- 每笔 signal date、entry quote timestamp、expiry、腿位；
- 实际结算价与来源；
- 被排除样本；
- 日期簇标识；
- 参数网格及完整结果；
- 提前退出逐日路径。

在用户会据此实盘的项目里，这些结果不能仅存在于一次临时分析会话中。

## 对 A–F 的直接判断

- A1：不能排除幸存者偏差；提高门槛到相似 n 不成立。
- A2：卖 bid/买 ask 本身保守，但这里的盘前盘口不可成交且跨周末陈旧，回测入口价无效。
- A3：遗漏对理论终值影响有限，对 $264 账户的可实现路径影响非常严重。
- A4：必须按日期簇；同一信号的多个到期先合成一笔，不能分别算独立样本。
- B：平仓成本算法正确；“提前平仓必然更差”的实盘结论没有被当前模拟支持。
- C1/C2：平均二项 Kelly 不适合该尾部分布和小样本。
- C3：是，`ok=True` 等于鼓励超配；应该返回拒绝。
- D：是，Bonferroni p=0.44 且 Fisher 未聚类，硬闸门过早。
- E：是，n=6 的点估计直接参与绿色硬判定，远超证据强度。
- F：`obs_day` 用于解释结构可以，用于交易 DTE 不可以；`px_on()` 向后取价是前瞻；EXPECTED_MOVE_TABLE 回用于全样本属于 in-sample 污染。

我没有修改代码。测试也未能运行：当前环境的系统 Python 没有安装 `pytest`；静态检查和快照核对不受影响。
