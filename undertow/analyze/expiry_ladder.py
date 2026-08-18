"""按到期日切片的近周持仓阶梯（确定性计算，无 I/O）。

动机（用户口径）:
  flow.py / gamma.py 把 60 天内所有到期日揉进 (行权价, C/P) 一格，方便看"整体墙在哪"，
  但短线做【某一个到期日】的价差（如 8/21 到期的 SLV 熊市看涨价差）时，需要看的是
  【那一天单独的持仓结构】——那天的 call/put OI 墙压在哪条、当天范围内谁在买谁在卖。
  不同到期日细节可能大不相同：当周周度可能全是卖方写权做压制，月度大到期却是买方囤 Call。

本模块干一件事：
  build_ladder(prev, curr) —— 选出【未来 3 个周五 + 最近月度 OPEX】这几个到期，
  对每个到期【单独过滤快照】后复用 analyze_gamma（墙位）+ analyze_flow（买卖方），
  产出逐到期的墙位 + ΔOI 买卖方明细，直接服务"我要做 X 日到期的价差"。

复用而非另起炉灶:
  * 墙位口径 = gamma.analyze_gamma（现价 ±WALL_BAND 内最大 call/put OI），与主报告一致。
  * 买卖方口径 = flow.analyze_flow（ΔOI × Delta 修正相对 IV + 绝对 IV 闸门），与主报告一致。
  单一到期的行权价数通常足够做相对化（周度大到期常有数十条），闸门/churning 折减同样生效。

诚实边界:
  * 仍是 ETF 代理链（SLV≈银、GLD≈金），行权价/IV 仅定性；周度到期越近 IV 噪音越大。
  * 只作波段级结构预警，非交易指令。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from undertow.core.models import OptionsSnapshot
from undertow.analyze.gamma import analyze_gamma, WALL_BAND
from undertow.analyze.flow import analyze_flow, FlowChange

# —— 到期选取 ——
LADDER_HORIZON_DAYS = 75      # 只在此窗口内选到期（超过即便有 OI 也不进阶梯）
N_WEEKLY_FRIDAYS = 3          # 未来几个周五（当周/下周/下下周）
MIN_EXPIRY_OI = 2_000        # 该到期 call+put 总 OI 低于此视为无意义（周度冷门），跳过
TOP_FLOW_ROWS = 8            # 逐到期买卖方表每侧最多上报几行


def _is_third_friday(d: date) -> bool:
    """月度标准 OPEX = 每月第三个周五（15–21 号之间的周五）。"""
    return d.weekday() == 4 and 15 <= d.day <= 21


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _friday_label(exp: date, ref: date) -> str:
    """按 ISO 周历差给周五到期贴标签（相对观察日 ref 所在周）。
    本周/下周/下下周更贴近人的口语；再远则用"N周后周五"。"""
    n = (_week_monday(exp) - _week_monday(ref)).days // 7
    return {0: "本周五", 1: "下周五", 2: "下下周五"}.get(n, f"{n}周后周五")


@dataclass(frozen=True)
class ExpirySlice:
    """单个到期日的独立持仓切片。"""

    expiry: date
    days_out: int
    label: str              # "本周五" / "下周五" / "下下周五" / "月度OPEX"
    is_monthly: bool        # 是否月度标准 OPEX（第三个周五）
    weekday_cn: str         # 周几（中文），周度到期未必都落周五时用于提示

    # 墙位（ETF 行权价口径；展示时由外部 conv 换算商品价）
    call_wall: float
    call_wall_oi: int
    put_wall: float
    put_wall_oi: int
    call_walls_top: list = field(default_factory=list)   # [(strike, oi)] OI 降序
    put_walls_top: list = field(default_factory=list)

    total_call_oi: int = 0
    total_put_oi: int = 0
    pcr: float = 0.0        # put/call OI 比（该到期整体）

    # 逐 ΔOI 买卖方（需 ≥2 天快照；仅一份则为空、has_flow=False）
    has_flow: bool = False
    flow_tilt: str = ""
    changes: list = field(default_factory=list)   # FlowChange，按 |ΔOI| 降序
    net_call_doi: int = 0   # ΔOI 求和（call 增仓净额）
    net_put_doi: int = 0


def _select_expiries(curr: OptionsSnapshot, today: date) -> list[date]:
    """选出未来 N 个周五 + 最近月度 OPEX（去重、有 OI、在窗口内）。"""
    # 各到期的总 OI，用于过滤冷门
    oi_by_exp: dict[date, int] = {}
    for c in curr.contracts:
        if c.open_interest > 0:
            oi_by_exp[c.expiry] = oi_by_exp.get(c.expiry, 0) + c.open_interest

    def _ok(e: date) -> bool:
        d = (e - today).days
        # d>=1：跳过当日到期(0DTE)——无法据此建多日价差，且带内 OI 常已打空
        return 1 <= d <= LADDER_HORIZON_DAYS and oi_by_exp.get(e, 0) >= MIN_EXPIRY_OI

    fridays = sorted(e for e in oi_by_exp if e.weekday() == 4 and _ok(e))
    weeklies = fridays[:N_WEEKLY_FRIDAYS]
    # 最近月度：第一个第三周五；若已在 weeklies 里则取其后的下一个月度
    monthlies = [e for e in fridays if _is_third_friday(e)]
    nearest_monthly = next((e for e in monthlies if e not in weeklies), None)
    targets = list(weeklies)
    if nearest_monthly is not None:
        targets.append(nearest_monthly)
    return sorted(set(targets))


def _filter_to_expiry(snap: OptionsSnapshot, exp: date) -> OptionsSnapshot:
    return replace(snap, contracts=[c for c in snap.contracts if c.expiry == exp])


_WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")


def build_ladder(
    prev: OptionsSnapshot | None,
    curr: OptionsSnapshot,
    *,
    today: date,
    multiplier: float,
    proxy_quality: str = "good",
) -> list[ExpirySlice]:
    """构建近周到期阶梯：逐到期独立跑墙位 + 买卖方。"""
    targets = _select_expiries(curr, today)
    slices: list[ExpirySlice] = []
    for exp in targets:
        c_e = _filter_to_expiry(curr, exp)
        if not c_e.with_oi():
            continue
        # 墙位：复用 analyze_gamma（同口径），horizon 给足以免该到期被窗口剔除
        horizon = max(LADDER_HORIZON_DAYS, (exp - today).days + 1)
        ga_e = analyze_gamma(c_e, multiplier=multiplier, proxy_quality=proxy_quality,
                             today=today, horizon_days=horizon)
        # 现价带内两侧墙均无 OI = 该到期在现价附近无持仓可依（死到期/远离现价），跳过
        # ——必须在分配周五标签(消费 wi)之前跳过，否则后续周五标签错位
        if ga_e.call_wall_oi == 0 and ga_e.put_wall_oi == 0:
            continue
        monthly = _is_third_friday(exp)
        # 标签：按 ISO 周历差贴 本周/下周/下下周五；再远的月度锚点直接标 月度OPEX
        if exp.weekday() == 4:
            n = (_week_monday(exp) - _week_monday(today)).days // 7
            if n <= 2:
                label = _friday_label(exp, today) + ("·月度OPEX" if monthly else "")
            elif monthly:
                label = "月度OPEX"
            else:
                label = f"{n}周后周五"
        else:
            label = f"周{_WEEKDAY_CN[exp.weekday()]}"

        total_c = sum(c.open_interest for c in c_e.contracts if c.is_call)
        total_p = sum(c.open_interest for c in c_e.contracts if not c.is_call)
        pcr = (total_p / total_c) if total_c > 0 else 0.0

        # 买卖方：复用 analyze_flow（同口径），prev 同样过滤到该到期
        has_flow = False
        flow_tilt = ""
        changes: list[FlowChange] = []
        net_c = net_p = 0
        if prev is not None:
            p_e = _filter_to_expiry(prev, exp)
            fa_e = analyze_flow(p_e, c_e, today=today, horizon_days=horizon,
                                call_wall=ga_e.call_wall, put_wall=ga_e.put_wall)
            if fa_e.prev_date is None:
                # 过滤后该到期昨日无合约（新挂到期）——仍展示墙位，买卖方留空
                has_flow = bool(fa_e.changes)
            else:
                has_flow = True
            flow_tilt = fa_e.flow_tilt
            changes = sorted(fa_e.changes, key=lambda x: -abs(x.d_oi))
            net_c = fa_e.net_call_doi
            net_p = fa_e.net_put_doi

        slices.append(ExpirySlice(
            expiry=exp, days_out=(exp - today).days, label=label, is_monthly=monthly,
            weekday_cn=_WEEKDAY_CN[exp.weekday()],
            call_wall=ga_e.call_wall, call_wall_oi=ga_e.call_wall_oi,
            put_wall=ga_e.put_wall, put_wall_oi=ga_e.put_wall_oi,
            call_walls_top=ga_e.call_walls_top, put_walls_top=ga_e.put_walls_top,
            total_call_oi=total_c, total_put_oi=total_p, pcr=pcr,
            has_flow=has_flow, flow_tilt=flow_tilt, changes=changes,
            net_call_doi=net_c, net_put_doi=net_p,
        ))
    return slices
