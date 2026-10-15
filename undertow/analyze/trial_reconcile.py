"""实盘试行：券商资金流水 + 成交 → 逻辑交易的自动对账（只读候选；纯函数，无 I/O）。

v2.2（Codex 050）：① 结果只是候选 —— CLI 的 `--apply` 不再写正式交易结果/覆盖时点（旧版会 record_trade(complete=True) +
verify_trades，下游 consecutive_losses 照常消费，自动推定就成了正式风控来源）；正式登记仍走人工 trade / verify-trades。
② 规则 11 的历史豁免要看覆盖证据类型（coverage_kind），不再只看覆盖时间：旧版先由自动 apply 推进覆盖，再出现一条覆盖前的
未知调整行，就从阻断降成 history_issues —— 自动覆盖被当成了人工前提。

v2.1（Codex 049）：① 期权流水金额符号必须与类型一致（买入负、卖出正），零额/反号不猜成普通成交（可能是冲正）；
② 成交与持仓张数必须是有限整数（"1.0" 可兼容，0.7 不再被 round 成 1）；③ 美东交易日先换算到 America/New_York 再取日期
（旧版按输入本地日期猜偏移：同一瞬间 22:00Z 与次日 06:00+08 被分进 10/15、10/16 两个费用桶）；
④ 读数证据要自证：必要源齐全、各段日期首尾相接覆盖起点到 now、计数与传入行数一致，空 reads 不再当完整；
⑤ 费用按 FIFO 归属时，若交换归属会改变逐笔结果（费用行没有成交日关联，原始行兼容两种解释）→ 阻断。

v2（Codex 048 P1-2/3/5）。v1 的错：以「合约」为节点、同秒连边 —— 同一合约四次完整往返被压成一笔（−12×4 → 一条 −48），
已登记首轮后其余三轮以同一 id「已登记」被跳过、覆盖照样推进；任意标的的成交只要 ±2 秒内有任一期权流水就算「对上」；
0 价成交不核到期日就当到期；非 USD / NaN 金额照算；孤立费用被忽略；同一笔已登记结果后来金额变了也不报。

2026-10-15 实测长桥只读 CLI（不外推为接口永远如此）：
- `order executions [--history]`：实际字段 order_id / price / quantity / side / symbol / time；schema 列的 trade_id 实际不返回，
  没有逐成交稳定 id。单腿单 symbol 是合约代码；组合单 symbol 是标的（SLV.US），每次成交出一组行：净价一行 + 各腿价一行，
  side 是整单方向（不是腿方向），张数相同。执行与流水时刻可差 1 秒；同秒同价同量的重复行是真实成交，不能去重。
- `cash-flow`：每腿一行，带合约（旧行在 description「OP/US/…」）、带符号金额（= 腿价 × 张数 × 100）、币种，没有张数；
  费用另起行，按（合约, 方向）合并，结算日才入账。
- 到期作废：成交记录里 0 价、合约代码的一行（到期次日凌晨），流水没有对应行。
- `order --history` 实测会静默截断（6/1 起查只返回到 8/28；--limit/--page 不生效）；`order executions` 与 `cash-flow`
  长区间和按周分段逐行一致，未见截断，但接口不给 has_more —— 只能用「每段行数远低于可疑上限」做保守检查。

规则（v2，写定）：
1. 张数：每条期权流水在 ±TIME_TOL_S 内找同标的根、同订单、同价格的成交行，要求其中一组行的张数之和 Q 满足
   Q × 价 × 100 = |金额|（差 ≤ AMT_TOL；部分成交会被流水合并成一行，实测 8/20 一条 −1078 = 1.54 × 7 张由 6 行成交拼成）；
   单腿行还要求合约与方向一致。能配上的（订单, 价格）不唯一 → 不确定。成交行用过即不再用。
2. 成交行全部要被解释：腿行（上条配上）、组合净价行（同订单 ±2 秒内已配腿各合约张数相等为 Q，净价行张数之和 = Q，
   每行净价 = |Σ 卖腿均价 − Σ 买腿均价|）、到期行（0 价、合约代码、时刻在该合约到期日 16:00 美东之后、≤ now、
   张数正好把推算持仓归零）。其余 → 不确定；到期前的 0 价 → 不确定。
3. 持仓：按时间逐合约累加带符号张数（买 +、卖 −；到期行把持仓归零），切出每段「从 0 到 0」的生命周期。
   一次成交跨过 0（+3 → −2）→ 不确定。推算的现时持仓必须与券商持仓逐合约相等，否则不确定。
4. 逻辑交易：同一订单号触及的生命周期段连成一笔（不同订单、不同标的即使同秒也不相连）。
   笔内有腿在其它腿平掉之后才开（换腿/滚仓）→ 不确定；同标的同到期的两笔时间重叠（可能分腿建仓/分批）→ 不确定。
5. 费用：每个（合约, 方向, 美东交易日）需要一条同合约同方向、时间不早于该日该方向最后成交的费用行（按时间先到先配）。
   没配上 → 费用未齐（不登记）；配不上任何成交日的费用行（孤立/迟到补费/合并）、正数费用（退款/调整）→ 不确定；
   同合约同方向同日的成交分属两笔交易 → 合并收费无法拆分 → 不确定。
   这只是「每个交易日每个方向见到一条」的推定，不是券商的结算证明（待 Codex 审）。
6. 金额：期权行币种必须 USD、金额必须有限，否则不确定（不按 USD 加总）。外币入金/换汇只列为外部现金流候选，不登记、不折算。
7. 试行到账之前有成交、又在窗口内继续成交的合约 → 不确定（起点持仓未知）。
8. 已登记交易：同 id 结果（净额/平仓时刻）不同 → 冲突；本来源登记过、这次却找不到的 id → 冲突。都不静默跳过。
   【假设，不是证明】「一桶见到一条费用行 = 费用已齐」与「先到先配」都是推定：费用行自身没有成交日/订单关联。
   v2.1 起，若存在另一种同样合法的归属（较早费用行也能配给较晚的桶、且两条金额不同）会改变某两笔的净额 → 归属不可识别，
   该两笔标为不确定（不登记）。金额相同或只有一种合法归属时才放行；放行的结果仍是「在一桶一条即齐的推定下」，
   未经人工或独立结算关联核对，不应被当成决定连续亏损门控的正式来源（见 evidence.assumptions）。
9. 读数：CLI 传入每段读取记录。必须有 cash-flow、executions（日期段）与 executions-today 三类；日期段首尾相接
   （下一段起点 = 上一段终点 + 1 天，无缺口无重叠）、覆盖试行起点日到 now 日；n 为非负整数；cash-flow 段行数之和 = 传入流水行数，
   executions 段行数之和（+ 当日行数，当日整体重叠时不加）= 传入成交行数。缺一项 → 不确定。任一段读取未完成 → 不确定。
   任一段 ≥ READ_SUSPECT_ROWS 行 → 不确定：150 是从 order --history 截断现象借来的【未校准提示】，不是 executions/cash-flow
   接口上限的证明；段行数远低于它也只是降低已见截断模式的风险。接口不给 has_more，所以通过的读数证据等级只是
   「读完本次声明的区间」，不是「券商源绝对完整」。
10. 只有阻断项为零（can_advance_coverage）时才给出候选记录；部分可信时一律只读候选，不部分列出。
    v2.2 起候选记录不由本模块或 CLI 写入正式状态：要计入连续亏损，须人工逐笔对照流水 `trial trade` 并 `verify-trades`。
11. 问题的阻断范围：读数、持仓不符、券商持仓不可用、已登记冲突、没有时刻的问题 → 永远阻断；
    其余问题只在其时刻晚于覆盖时点、或涉及在场/覆盖后交易的合约时阻断；完全落在覆盖时点之前、
    且不涉及这些合约的，【仅当】调用方传入的覆盖证据类型是 manual_verified（人工 verify-trades）或 funding_start
    （覆盖点就是到账起点，之前没有试行交易）时才列为 history_issues（照样显示）；automatic / unknown（含旧状态缺字段）
    → 照样阻断：自动/来源不明的覆盖不能循环背书自己。前提由结构化字段传入（trial_capital.coverage_kind），不匹配中文字样。
    这只是工程上的豁免，【不】证明历史逐笔盈亏/费用/笔数正确 —— 现时持仓相等不能证明历史：漏掉一整次往返照样平到 0。
    豁免不用于解除既有暂停；已知交易冲突、跨覆盖仍在场的合约、没有时刻的问题照旧硬阻断。
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCHEMA = "trial-reconcile-v2.2"
SOURCE = "券商资金流水自动对账（trial-reconcile-v2）"
SOURCES_AUTO = frozenset({SOURCE, "券商资金流水自动对账（trial-reconcile-v1）"})
TIME_TOL_S = 2
AMT_TOL = 0.011
PX_TOL = 0.006
READ_SUSPECT_ROWS = 150          # 未校准提示：借自 order --history 在 180–230 行处静默截断的现象；不是另两个接口上限的证明
_ET = ZoneInfo("America/New_York")
HISTORY_EXEMPT_KINDS = frozenset({"manual_verified", "funding_start"})   # 规则 11：只有这两类覆盖前提可豁免覆盖前问题
ASSUMPTIONS = (
    "读数：只证明读完了本次声明的日期段（首尾相接、计数一致）；接口不给 has_more，不证明券商源绝对完整",
    "费用齐全：每（合约, 方向, 美东交易日）见到一条费用行即视为已齐（推定，非券商结算证明）",
    "费用归属：先到先配（FIFO）；交换归属会改变逐笔结果时已阻断，放行的结果仍依赖上一条推定",
    "历史豁免：只在覆盖证据类型为人工核查（或到账起点）时，覆盖时点之前的问题才不阻断；不证明历史逐笔结果",
)

_OCC = re.compile(r"([A-Za-z]+)(\d{6})([CP])(\d+)")
OPTION_TXN = {"Option Buy Transaction": "buy", "Option Sell Transaction": "sell"}
OPTION_FEE = {"Option Buy Fee": "buy", "Option Sell Fee": "sell"}
EXTERNAL = {"Cash Deposit", "Cash Withdrawal"}
KNOWN_OTHER = {"Currency Conversion (Credit)", "Currency Conversion (Debit)", "Reward Redemption",
               "Cash Plus Subscription", "Cash Plus Redemption", "Debit Interest",
               "Corporate Action Funds Debit", "Corporate Action Funds Credit"}


def _ts(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def _fnum(x) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def contract_of(row: dict) -> str | None:
    """合约代码（不带 .US）。symbol 优先，旧行从 description 里取；都没有 → None。"""
    for field in ("symbol", "description"):
        v = str(row.get(field) or "")
        m = _OCC.fullmatch(v.split("/")[-1].split(".")[0]) if v else None
        if m:
            return "".join(m.groups())
    return None


def root_of(symbol: str) -> str:
    c = contract_of({"symbol": symbol})
    return (_OCC.fullmatch(c).group(1) if c else str(symbol or "").split(".")[0]).upper()


def parse_contract(code: str) -> tuple[str, date, str, float]:
    root, ymd, cp, k = _OCC.fullmatch(code).groups()
    return root.upper(), date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), cp, int(k) / 1000.0


def expiry_close_utc(d: date) -> datetime:
    """到期日 16:00 美东 → UTC（标准库 zoneinfo，按美东本地日期判夏令时）。"""
    return datetime(d.year, d.month, d.day, 16, tzinfo=_ET).astimezone(timezone.utc)


def _et_date(t: datetime) -> date:
    """带时区时刻 → 美东日历日。先换算到 America/New_York 再取日期：同一瞬间不论用 Z/+08/−04 表示都落同一天，
    夏令时切换按瞬间判（Codex 049：旧版按输入本地日期猜偏移，22:00Z 与次日 06:00+08 分进两天）。"""
    return t.astimezone(_ET).date()


def _int_qty(v) -> int | None:
    """张数：有限且为整数（"1"、"1.0" 可以；0.7 不 round）。"""
    q = _fnum(v)
    return int(q) if q is not None and q == int(q) else None


def _check_reads(reads, start: datetime, t_now: datetime, n_flows: int, n_exs: int) -> list[str]:
    """读数证据（规则 9）。返回问题列表（全部硬阻断）；空列表 = 读完了本次声明的区间（不等于券商源绝对完整）。"""
    if reads is None:
        return ["未提供分段读数记录：无法确认读取完整"]
    probs = []
    for r in reads:
        n = r.get("n")
        if not r.get("ok", False):
            probs.append(f"{r.get('what')} {r.get('start')}–{r.get('end')} 读取未完成")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            probs.append(f"{r.get('what')} {r.get('start')}–{r.get('end')} 行数 {n!r} 不是非负整数")
        elif n >= READ_SUSPECT_ROWS:
            probs.append(f"{r.get('what')} {r.get('start')}–{r.get('end')} 一段 {n} 行 ≥ {READ_SUSPECT_ROWS}"
                         "（未校准提示）：可能被截断")
    counts = {}
    for what in ("cash-flow", "executions"):
        segs = [r for r in reads if r.get("what") == what]
        if not segs:
            probs.append(f"读数记录缺少 {what}：无法确认该源已读")
            continue
        try:
            span = sorted((date.fromisoformat(r["start"]), date.fromisoformat(r["end"])) for r in segs)
        except (KeyError, TypeError, ValueError):
            probs.append(f"{what} 读段起止日期不可解析")
            continue
        if any(a > b for a, b in span):
            probs.append(f"{what} 有起点晚于终点的读段")
        for (_, b0), (a1, _) in zip(span, span[1:]):
            if a1 != b0 + timedelta(days=1):
                probs.append(f"{what} 读段 {b0} 与 {a1} 之间{'有缺口' if a1 > b0 + timedelta(days=1) else '重叠/重复'}")
        if span[0][0] > start.astimezone(timezone.utc).date() or span[-1][1] < t_now.astimezone(timezone.utc).date():
            probs.append(f"{what} 读段 {span[0][0]}–{span[-1][1]} 没覆盖试行起点到 now")
        counts[what] = sum(r["n"] for r in segs if isinstance(r.get("n"), int) and not isinstance(r.get("n"), bool))
    today = [r for r in reads if r.get("what") == "executions-today"]
    if len(today) != 1:
        probs.append(f"读数记录里当日成交段有 {len(today)} 条（应为 1）")
    if "cash-flow" in counts and counts["cash-flow"] != n_flows:
        probs.append(f"cash-flow 读段行数之和 {counts['cash-flow']} ≠ 传入流水 {n_flows} 行")
    if "executions" in counts and len(today) == 1 and isinstance(today[0].get("n"), int):
        if n_exs not in (counts["executions"], counts["executions"] + today[0]["n"]):
            probs.append(f"executions 读段行数 {counts['executions']}（当日 {today[0]['n']}）与传入成交 {n_exs} 行对不上")
    return probs


def _near(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) <= TIME_TOL_S


def reconcile(*, cash_flow: list[dict], positions: dict, executions: list[dict], coverage_through: str,
              trial_start: str, now: str, known_trades: dict | None = None, reads: list[dict] | None = None,
              coverage_kind: str = "unknown") -> dict:
    """→ 对账候选（见模块头规则）。

    cash_flow / executions：试行到账起至 now 的原样行（可含缓冲日之外的行，按时间裁剪）；
    positions：{代码(.US 可带): 带符号张数}（券商当前持仓）；known_trades：状态里的 closed_trades（id → 记录）；
    reads：[{"what","start","end","n","ok"}] 每段读数（None = 未提供 → 不可推进）；
    coverage_kind：覆盖证据类型（trial_capital.COVERAGE_KINDS），缺省 unknown → 不给历史豁免（规则 11）。"""
    start, cov, t_now = _ts(trial_start), _ts(coverage_through), _ts(now)
    out = {"schema": SCHEMA, "status": "incomplete", "groups": [], "records": [], "external": [], "other": [],
           "unknown": [], "problems": [], "history_issues": [], "notes": [], "conflicts": [], "blockers": [],
           "expiry_events": [], "can_advance_coverage": False}
    if start is None or cov is None or t_now is None:
        out["problems"] = out["blockers"] = ["试行起点/覆盖时点/当前时刻缺失或不带时区"]
        return out
    problems: list[dict] = []
    notes: list[str] = []
    known = known_trades or {}

    def P(msg, t=None, cs=(), hard=False):
        """问题。hard / 无时刻 → 永远阻断；否则只在时刻晚于覆盖时点、或涉及在场/覆盖后交易的合约时阻断（见规则 11）。"""
        problems.append({"msg": msg, "t": t, "contracts": tuple(c for c in cs if c), "hard": hard})

    # 9. 读数证据
    read_probs = _check_reads(reads, start, t_now, len(cash_flow), len(executions))
    for msg in read_probs:
        P(msg, hard=True)

    # 解析流水
    txns, fees = [], []
    pre_trial = set()
    for i, r in enumerate(cash_flow):
        name, t = r.get("flow_name"), _ts(r.get("time"))
        if t is None:
            P(f"流水行时间不可解析：{name!r}", hard=True)
            continue
        if t > t_now:
            continue
        amt = _fnum(r.get("balance"))
        if name in OPTION_TXN or name in OPTION_FEE:
            c = contract_of(r)
            if c is None:
                P(f"期权流水认不出合约：{name} @ {r.get('time')}", t)
                continue
            if t < start:
                if name in OPTION_TXN:
                    pre_trial.add(c)
                continue
            if amt is None:
                P(f"期权流水金额不是有限数：{c} {name} @ {r.get('time')}", t, [c])
                continue
            if r.get("currency") != "USD":
                P(f"期权流水币种 {r.get('currency')!r} 不是 USD：{c} {name} @ {r.get('time')}（不按 USD 加总）", t, [c])
                continue
            want = {"buy": -1, "sell": 1}.get(OPTION_TXN.get(name), -1)      # 成交：买负卖正；费用：负
            if amt == 0 or (amt > 0) != (want > 0):
                if name in OPTION_FEE and amt > 0:
                    pass                                       # 正数费用在规则 5 里单独报（退款/调整）
                else:
                    P(f"期权流水金额 {amt:+.2f} 与类型 {name} 的符号不符或为零（可能是冲正/退款/调整）：{c} @ {r.get('time')}，"
                      "不当普通成交/费用", t, [c])
                    continue
            rec = {"i": i, "contract": c, "root": parse_contract(c)[0], "t": t, "amount": amt,
                   "side": OPTION_TXN.get(name) or OPTION_FEE.get(name), "qty": None, "order": None, "px": None}
            (txns if name in OPTION_TXN else fees).append(rec)
        elif t < start:
            continue
        else:
            row = {"flow_name": name, "currency": r.get("currency"), "amount": amt, "time": r.get("time")}
            if name in EXTERNAL:
                out["external"].append(row)
            elif name in KNOWN_OTHER:
                out["other"].append(row)
            else:
                out["unknown"].append({**row, "symbol": r.get("symbol")})
                P(f"未知流水类型 {name!r} {r.get('symbol') or ''} @ {r.get('time')}（可能是行权/指派/股票成交）", t,
                  [contract_of(r)])

    # 解析成交
    exs = []
    for j, e in enumerate(executions):
        t = _ts(e.get("time"))
        if t is None:
            P("成交行时间不可解析", hard=True)
            continue
        if t < start or t > t_now:
            continue
        q, px = _int_qty(e.get("quantity")), _fnum(e.get("price"))
        if q is None or q <= 0 or px is None or px < 0 or not e.get("order_id"):
            P(f"成交行张数/价格/订单号不可用（张数须为正整数）：{e.get('symbol')} 张数 {e.get('quantity')!r} @ {e.get('time')}",
              t, [contract_of({"symbol": e.get("symbol")})])
            continue
        sym = str(e.get("symbol") or "")
        exs.append({"j": j, "t": t, "order": str(e["order_id"]), "symbol": sym, "contract": contract_of({"symbol": sym}),
                    "root": root_of(sym), "side": str(e.get("side") or "").lower(), "qty": q, "px": px, "role": None})

    # 1+2. 流水 ↔ 成交：按订单整体求解。每条流水的候选 =（订单, 价格, 张数 Q）：同标的根、±2 秒、同订单同价格的若干行
    #      张数之和为 Q 且 Q × 价 × 100 = |金额|（部分成交会被流水合并成一行）。同一订单簇里枚举各流水的候选组合，
    #      要求成交行不重复使用、且该订单剩下的标的代码行恰好构成组合净价行（各合约腿张数相等为 Q，净价行张数之和 = Q，
    #      每行净价 = |Σ 卖腿均价 − Σ 买腿均价|）。恰好一种解释成立才采用；零种 → 配不上；多种 → 不唯一。
    def _subset(rows, target):
        best = {0: []}
        for e in rows:
            q = e["qty"]                                       # 解析时已保证正整数
            for tot, pick in list(best.items()):
                if tot + q <= target and tot + q not in best:
                    best[tot + q] = pick + [e]
        return best.get(target)

    def _pool(x, o, px, used):
        return [e for e in sorted(exs, key=lambda e: (abs((e["t"] - x["t"]).total_seconds()), e["j"]))
                if e["j"] not in used and e["order"] == o and round(e["px"], 4) == px and e["root"] == x["root"]
                and _near(e["t"], x["t"]) and (e["contract"] is None or (e["contract"] == x["contract"] and e["side"] == x["side"]))]
    opts = {}
    for x in txns:
        cand = set()
        for e in exs:
            if e["px"] > 0 and e["root"] == x["root"] and _near(e["t"], x["t"]) and \
                    (e["contract"] is None or (e["contract"] == x["contract"] and e["side"] == x["side"])):
                px = round(e["px"], 4)
                q = round(abs(x["amount"]) / (px * 100))
                if q >= 1 and abs(q * px * 100 - abs(x["amount"])) <= AMT_TOL and _subset(_pool(x, e["order"], px, set()), q):
                    cand.add((e["order"], px, q))
        opts[x["i"]] = sorted(cand)
        if not cand:
            P(f"流水 {x['contract']} {x['side']} {x['amount']:+.2f} @ {x['t'].isoformat()}：找不到价×量相符的成交行",
              x["t"], [x["contract"]])
    # 订单簇：共享候选订单的流水连在一起
    xs = [x for x in sorted(txns, key=lambda r: (r["t"], r["i"])) if opts[x["i"]]]
    cl_parent = {x["i"]: x["i"] for x in xs}

    def cf(a):
        while cl_parent[a] != a:
            a = cl_parent[a]
        return a
    first_by_order = {}
    for x in xs:
        for o, _, _ in opts[x["i"]]:
            if o in first_by_order:
                cl_parent[cf(x["i"])] = cf(first_by_order[o])
            else:
                first_by_order[o] = x["i"]
    clusters = defaultdict(list)
    for x in xs:
        clusters[cf(x["i"])].append(x)
    for members in clusters.values():
        orders = {o for x in members for o, _, _ in opts[x["i"]]}
        combos, n = [[]], 1
        for x in members:
            n *= len(opts[x["i"]])
        if n > 4096:
            for x in members:
                P(f"流水 {x['contract']} @ {x['t'].isoformat()}：同订单簇候选组合过多（{n}），无法唯一解释", x["t"], [x["contract"]])
            continue
        for x in members:
            combos = [c + [op] for c in combos for op in opts[x["i"]]]
        valid = []
        for combo in combos:
            used, pick_of = set(), {}
            okc = True
            for x, (o, px, q) in zip(members, combo):
                pick = _subset(_pool(x, o, px, used), q)
                if not pick:
                    okc = False
                    break
                used |= {e["j"] for e in pick}
                pick_of[x["i"]] = (o, px, q, pick)
            if not okc:
                continue
            nets = []
            for o in orders:
                legs = [(x, pick_of[x["i"]]) for x in members if pick_of[x["i"]][0] == o]
                left = [e for e in exs if e["order"] == o and e["j"] not in used and e["px"] > 0
                        and any(_near(e["t"], x["t"]) for x, _ in legs)]
                if not legs:
                    continue
                if not left:
                    continue
                qc, amt, side = defaultdict(float), defaultdict(float), {}
                for x, (_, _, q, _) in legs:
                    qc[x["contract"]] += q
                    amt[x["contract"]] += abs(x["amount"])
                    side[x["contract"]] = x["side"]
                Q = next(iter(qc.values()))
                net = abs(sum((1 if side[c] == "sell" else -1) * amt[c] / (100 * qc[c]) for c in qc))
                if len(qc) < 2 or len({round(v, 9) for v in qc.values()}) != 1 or any(e["contract"] is not None for e in left) \
                        or abs(sum(e["qty"] for e in left) - Q) > 1e-9 or any(abs(e["px"] - net) > PX_TOL for e in left):
                    okc = False
                    break
                nets += left
            if okc:
                valid.append((tuple((x["i"],) + combo[k] for k, x in enumerate(members)), pick_of, nets))
        sigs = {v[0] for v in valid}
        if len(sigs) == 1:
            _, pick_of, nets = valid[0]
            for x in members:
                o, px, q, pick = pick_of[x["i"]]
                for e in pick:
                    e["role"] = "leg"
                x["qty"], x["order"], x["px"] = float(q), o, px
            for e in nets:
                e["role"] = "net"
        else:
            for x in members:
                P(f"流水 {x['contract']} {x['side']} {x['amount']:+.2f} @ {x['t'].isoformat()}："
                  + ("与同订单其它行无法同时解释（净价行对不上）" if not sigs else f"同订单簇有 {len(sigs)} 种解释，无法唯一确定张数"),
                  x["t"], [x["contract"]])

    # 3. 持仓路径（含到期行），切生命周期段
    timeline = [{"t": x["t"], "contract": x["contract"], "dq": x["qty"] if x["side"] == "buy" else -x["qty"],
                 "order": x["order"], "amount": x["amount"], "kind": "txn", "side": x["side"]} for x in txns if x["qty"]]
    timeline += [{"t": e["t"], "contract": e["contract"], "exp": e, "kind": "expiry"}
                 for e in exs if e["role"] is None and e["px"] == 0 and e["contract"] is not None]
    timeline.sort(key=lambda v: (v["t"], v["kind"] == "expiry"))
    pos, open_seg, segs = defaultdict(float), {}, []
    for v in timeline:
        c, p0 = v["contract"], pos[v["contract"]]
        if v["kind"] == "expiry":
            e = v["exp"]
            if e["t"] < expiry_close_utc(parse_contract(c)[1]):
                P(f"到期前的 0 价成交 {c} @ {e['t'].isoformat()}：不是到期处理，性质未知", e["t"], [c])
                continue
            if p0 == 0 or abs((1 if e["side"] == "buy" else -1) * e["qty"] + p0) > 1e-9:
                P(f"0 价成交 {c} {e['side']} {e['qty']:g} 与推算持仓 {p0:g} 不符：到期处理无法证实", e["t"], [c])
                continue
            e["role"] = "expiry"
            out["expiry_events"].append({"contract": c, "time": e["t"].isoformat(), "qty": e["qty"]})
            v = {"t": e["t"], "contract": c, "dq": -p0, "order": f"expiry:{c}", "amount": 0.0, "kind": "expiry", "side": e["side"]}
        p1 = p0 + v["dq"]
        if p0 != 0 and p1 != 0 and (p0 > 0) != (p1 > 0):
            P(f"{c} 一次成交从 {p0:g} 跨到 {p1:g}（不经过 0）：生命周期无法切分", v["t"], [c])
        if p0 == 0:
            open_seg[c] = {"contract": c, "start": v["t"], "end": None, "events": []}
            segs.append(open_seg[c])
        open_seg[c]["events"].append(v)
        pos[c] = 0.0 if abs(p1) < 1e-9 else p1
        if pos[c] == 0.0:
            open_seg.pop(c)["end"] = v["t"]
    for e in exs:
        if e["role"] is None and not (e["px"] == 0 and e["contract"] is not None):
            P(f"成交行 {e['symbol']} {e['side']} {e['qty']:g}@{e['px']} @ {e['t'].isoformat()} 在流水里解释不了", e["t"], [e["contract"]])
    for c in sorted(pre_trial & {s["contract"] for s in segs}):
        P(f"{c} 在试行到账之前有成交：起点持仓未知，推算失真", max(v["t"] for s in segs if s["contract"] == c for v in s["events"]), [c])

    # 推算持仓 vs 券商持仓
    broker = defaultdict(float)
    for sym, q in (positions or {}).items():
        c = contract_of({"symbol": sym})
        if c is None:
            continue
        qv = _int_qty(q)
        if qv is None:
            P(f"券商持仓 {sym} 张数 {q!r} 不是有限整数", hard=True)
        else:
            broker[c] += qv
    for c in sorted(set(broker) | {c for c, q in pos.items() if q}):
        if abs(broker.get(c, 0.0) - pos.get(c, 0.0)) > 1e-9:
            P(f"持仓不符：{c} 券商 {broker.get(c, 0.0):g} vs 流水推算 {pos.get(c, 0.0):g}", hard=True)

    # 4. 逻辑交易 = 订单号连起来的生命周期段
    parent = list(range(len(segs)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    by_order = defaultdict(list)
    for k, s in enumerate(segs):
        for v in s["events"]:
            by_order[v["order"]].append(k)
    for ks in by_order.values():
        for k in ks[1:]:
            parent[find(k)] = find(ks[0])
    comps = defaultdict(list)
    for k in range(len(segs)):
        comps[find(k)].append(k)

    # 5. 费用：按（合约, 方向, 美东交易日）桶，先到先配
    bucket_comps, bucket_last = defaultdict(set), {}
    for r, ks in comps.items():
        for k in ks:
            for v in segs[k]["events"]:
                if v["kind"] == "txn":
                    b = (v["contract"], v["side"], _et_date(v["t"]))
                    bucket_comps[b].add(r)
                    bucket_last[b] = max(bucket_last.get(b, v["t"]), v["t"])
    bucket_fee = {}
    for f in sorted(fees, key=lambda f: (f["t"], f["i"])):
        if f["amount"] > 0:
            P(f"正数费用（退款/调整）{f['contract']} {f['amount']:+.2f} @ {f['t'].isoformat()}：类别不明", f["t"], [f["contract"]])
            continue
        cands = sorted(b for b in bucket_last if b[0] == f["contract"] and b[1] == f["side"] and b not in bucket_fee
                       and bucket_last[b] <= f["t"])
        if cands:
            bucket_fee[cands[0]] = f
        elif f["contract"] in pre_trial and f["contract"] not in {s["contract"] for s in segs}:
            notes.append(f"费用行属于试行前成交的合约 {f['contract']} {f['amount']:+.2f} @ {f['t'].isoformat()}：不归入试行交易")
        else:
            P(f"费用行配不上任何成交日（孤立/迟到补费/合并）：{f['contract']} {f['side']} "
              f"{f['amount']:+.2f} @ {f['t'].isoformat()}", f["t"], [f["contract"]])

    # 5b. 费用归属可识别性：同（合约, 方向）两条已配费用 f_i 早于 f_j，若 f_j 配给的桶在 f_i 入账时也已成交完（同样合法），
    #     且两条金额不同、两桶属于不同的笔 → 交换归属会改变两笔的净额，原始行无法区分 → 两桶标为归属不可识别。
    fee_ambig = set()
    by_cs = defaultdict(list)
    for b, f in bucket_fee.items():
        by_cs[(b[0], b[1])].append((f["t"], f["i"], b, f))
    for lst in by_cs.values():
        lst.sort(key=lambda x: (x[0], x[1]))
        for a, (ti, _, bi, fi) in enumerate(lst):
            for _, _, bj, fj in lst[a + 1:]:
                if abs(fi["amount"] - fj["amount"]) > 0.005 and bucket_last[bj] <= ti and bucket_comps[bi] != bucket_comps[bj]:
                    fee_ambig |= {bi, bj}

    groups = []
    for r, ks in comps.items():
        ss = [segs[k] for k in ks]
        evs = sorted((v for s in ss for v in s["events"]), key=lambda v: v["t"])
        cs = sorted({s["contract"] for s in ss})
        und = sorted({parse_contract(c)[0] for c in cs})
        exps = sorted({str(parse_contract(c)[1]) for c in cs})
        closed = all(s["end"] is not None for s in ss)
        flags = []
        if len(ss) > 1 and max(s["start"] for s in ss) >= min(s["end"] or t_now for s in ss):
            flags.append("笔内有腿在其它腿平掉之后才开（换腿/滚仓）：是否同一笔需人工确认")
        mine = [b for b, rs in bucket_comps.items() if r in rs]
        flags += [f"{b[0]} {b[1]} {b[2]} 的成交分属多笔：合并收费无法拆分" for b in mine if len(bucket_comps[b]) > 1]
        flags += [f"{b[0]} {b[1]} {b[2]} 的费用归属不可识别：另一种同样合法的归属会改变逐笔净额（费用行无成交日关联）"
                  for b in sorted(mine) if b in fee_ambig]
        pend = [f"{b[0]} {b[1]} {b[2]}" for b in mine if b not in bucket_fee]
        fee_amt = round(sum(bucket_fee[b]["amount"] for b in mine if b in bucket_fee), 2)
        gross = round(sum(v["amount"] for v in evs), 2)
        first_order = next((v["order"] for v in evs if v["kind"] == "txn"), evs[0]["order"])
        g = {"contracts": cs, "underlying": "/".join(und), "expiries": exps, "segments": len(ss),
             "first_txn": evs[0]["t"].isoformat(), "last_txn": evs[-1]["t"].isoformat(), "flags": flags,
             "state": "closed" if closed else "open", "gross_usd": gross, "fees_usd": fee_amt,
             "fees_settled": not pend, "fees_pending": pend, "net_pnl_usd": None,
             "closed_at": evs[-1]["t"].isoformat() if closed else None,
             "trade_id": f"lb2|{'/'.join(und)}|{','.join(exps)}|{','.join(cs)}|"
                         f"{evs[0]['t'].strftime('%Y%m%dT%H%M%SZ')}|{first_order}"}
        if closed and not pend:
            g["net_pnl_usd"] = round(gross + fee_amt, 2)
        groups.append(g)
    groups.sort(key=lambda g: g["first_txn"])
    for i, a in enumerate(groups):
        for b in groups[i + 1:]:
            if a["underlying"] == b["underlying"] and set(a["expiries"]) & set(b["expiries"]):
                if _ts(a["first_txn"]) <= (_ts(b["closed_at"]) or t_now) and _ts(b["first_txn"]) <= (_ts(a["closed_at"]) or t_now):
                    for g in (a, b):
                        g["flags"].append("同标的同到期另有时间重叠的交易（可能分腿建仓或分批进出）：需人工确认")
    for g in groups:
        if g["flags"]:
            g["state"] = "ambiguous"

    # 8. 已登记结果核对
    by_id = {g["trade_id"]: g for g in groups}
    for tid, rec in known.items():
        g = by_id.get(tid)
        if g is None:
            if rec.get("source") in SOURCES_AUTO:
                out["conflicts"].append(f"已自动登记的 {tid} 在本次流水里找不到：数据或口径变了，需人工核对")
            continue
        old = _fnum(rec.get("net_pnl_usd"))
        if g["state"] != "closed" or g["net_pnl_usd"] is None:
            out["conflicts"].append(f"已登记的 {tid} 现在不是确定的已平结果（{g['state']}）")
        elif old is None or abs(old - g["net_pnl_usd"]) > 0.005 or rec.get("closed_at") != g["closed_at"]:
            out["conflicts"].append(f"已登记的 {tid} 结果变了：登记 {rec.get('net_pnl_usd')} @ {rec.get('closed_at')} → "
                                    f"现算 {g['net_pnl_usd']} @ {g['closed_at']}（迟到费用/流水修订？）")

    live = [g for g in groups if g["state"] != "closed" or _ts(g["closed_at"]) > cov]
    live_cs = {c for g in live for c in g["contracts"]}
    exempt_ok = coverage_kind in HISTORY_EXEMPT_KINDS
    blocking, history, denied = [], [], 0
    for p in problems:
        if p["hard"] or p["t"] is None or p["t"] > cov or set(p["contracts"]) & live_cs:
            blocking.append(p["msg"])
        elif exempt_ok:
            history.append(p["msg"])
        else:
            blocking.append(p["msg"]); denied += 1
    if denied:
        blocking.append(f"覆盖证据类型是 {coverage_kind}（非人工核查/到账起点）：覆盖时点之前的 {denied} 个问题不豁免（规则 11）")
    blockers = blocking + out["conflicts"]
    blockers += [f"{g['trade_id']}：{'；'.join(g['flags'])}" for g in live if g["state"] == "ambiguous"]
    blockers += [f"{g['trade_id']}：费用未齐（{', '.join(g['fees_pending'])}）" for g in live
                 if g["state"] == "closed" and not g["fees_settled"]]
    ok = not blockers
    out["evidence"] = {"reads": "insufficient" if read_probs else "declared-segments-read",
        "fees": "assumed-one-row-per-bucket", "fee_attribution": "fifo-swap-checked", "assumptions": list(ASSUMPTIONS),
        "coverage_kind": coverage_kind, "history_exemption": exempt_ok, "formal_state": "candidate-only"}
    out.update(groups=groups, problems=[p["msg"] for p in problems], history_issues=history, notes=notes,
               blockers=blockers, can_advance_coverage=ok,
               status="complete" if ok else "incomplete")
    if ok:
        out["records"] = [{"trade_id": g["trade_id"], "net_pnl_usd": g["net_pnl_usd"], "closed_at": g["closed_at"],
                           "complete": True, "already_recorded": g["trade_id"] in known}
                          for g in groups if g["state"] == "closed" and _ts(g["closed_at"]) > cov]
    return out
