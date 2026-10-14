"""实盘试行状态命令（Codex 043 WP3）：只读账户 + 私有状态文件，从不下单、不入金。

  undertow trial status                          # 试行状态、额度、暂停（读长桥权益，只读）
  undertow trial init --planned 500              # 建立试行（计划金额，不是到账事实）
  undertow trial fund --amount 500 --note "…"    # 记录用户在券商端已完成的到账（用户原话）
  undertow trial resume --note "…"               # 解除暂停：只按用户明确决定，记原话

状态在 data/account/live_trial/state.json（gitignore）。损坏 → 隔离并当场报告，冻结新增。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from undertow.analyze import risk_policy as rp
from undertow.analyze import trial_capital as tc
from undertow.collect import trial_store as ts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pause_view(equity, *, equity_complete: bool, asof: str, persist: bool = True) -> tuple[dict | None, str]:
    """给预算入口用的暂停视图；None = 未知/未建立/损坏（预算 → incomplete）。越线时把 latch 写盘。"""
    st, prob = ts.load()
    if prob:
        return None, f"⚠️ {prob} —— 冻结新增"
    if st is None:
        return None, "试行未建立（undertow trial init）：暂停状态未知，预算不放行"
    st2, view = tc.evaluate(st, equity=equity, equity_complete=equity_complete, asof=asof, policy=rp.POLICY_B)
    if persist and st2 != st:
        ts.save(st2)
    return view["pause"], view.get("note", "")


def cmd_status(args) -> int:
    from undertow.collect import longbridge_account as lb
    st, prob = ts.load()
    if prob:
        print(f"⚠️ {prob}\n新增一律冻结；请检查隔离文件后再决定。"); return 2
    if st is None:
        print("试行未建立。用 `undertow trial init --planned 500` 建立（计划金额，不是到账事实）。"); return 0
    try:
        equity = lb.fetch_assets().net_assets; complete = equity is not None
    except Exception as e:                                     # noqa: BLE001 —— 账户读不到 = 不可判定
        equity, complete = None, False
        print(f"账户不可用：{str(e)[:120]}")
    st2, v = tc.evaluate(st, equity=equity, equity_complete=complete, asof=_now(), policy=rp.POLICY_B)
    if st2 != st:
        ts.save(st2)
    P = rp.POLICY_B
    R = equity
    f = lambda x: "未知" if x is None else f"${x:,.2f}"
    print(f"试行 {st2['trial_id']} · 政策 {P['version']}")
    print(f"计划资本 {f(st2['planned_capital'])} · 实际到账 {f(st2['funded_capital'])} · 外部净入金 {f(v['net_external_flows'])}")
    print(f"当前权益 R {f(R)} · 起始资本亏损 {'—' if v['initial_capital_loss_frac'] is None else format(v['initial_capital_loss_frac'], '.1%')}"
          f" · 峰值回撤 {'—' if v['peak_drawdown_frac'] is None else format(v['peak_drawdown_frac'], '.1%')}（只报告）"
          f" · 连续亏损 {v['consecutive_losses'] if v['consecutive_losses'] is not None else '—'} 笔")
    pv = v["pause"]
    print("暂停：" + ("未知（不可判定 → 冻结新增）" if pv is None else ("🔴 已暂停：" + (pv["reason"] or "") if pv["latched"] else "未暂停")))
    if v.get("note"):
        print(v["note"])
    for name, rule in (("单笔计划止损", P["stop_loss"]), ("单笔理论最大亏损", P["max_loss"]),
                       ("金银合计", P["categories"]["metals"]), ("其它合计", P["categories"]["other"]), ("全场合计", P["account"])):
        print(f"  {name} ≤ {f(rp.limit_usd(rule, R))}（max({rule['frac']:.0%}·R, ${rule['floor_usd']:.0f})）")
    print(f"  大仓（理论最大亏损 > ${P['big_position']['threshold_usd']:.0f}）同时最多 {P['big_position']['max_count']} 笔；止盈 {P['take_profit_gain_frac']:.0%}")
    print("政策额度 ≠ 能开：价差权限、组合占用、到期/指派处置需在券商 App 核实。本工具只读，从不下单、不入金。")
    return 0


def cmd_init(args) -> int:
    st, prob = ts.load()
    if prob or st is not None:
        print(prob or f"试行 {st['trial_id']} 已存在，不覆盖"); return 1
    tid = f"trial-{datetime.now(timezone.utc):%Y%m%d}"
    ts.save(tc.new_state(trial_id=tid, planned_capital=float(args.planned), policy_version=rp.VERSION_B, created_at=_now()))
    print(f"已建立 {tid}：计划资本 ${float(args.planned):,.2f}（计划，不是到账）"); return 0


def cmd_fund(args) -> int:
    st, prob = ts.load()
    if prob or st is None:
        print(prob or "试行未建立"); return 1
    if not (args.note or "").strip():
        print("需要用户原话（--note）"); return 1
    ts.save(tc.record_funding(st, amount=float(args.amount), at=_now(), note=args.note))
    print("已记录到账（入金不解除暂停、不洗掉亏损）"); return 0


def cmd_resume(args) -> int:
    st, prob = ts.load()
    if prob or st is None:
        print(prob or "试行未建立"); return 1
    if not st["pause_latched"]:
        print("当前未暂停"); return 0
    ts.save(tc.resume(st, user_note=args.note, at=_now()))
    print("已按用户决定解除暂停；连续亏损从此刻重新计数"); return 0


def register(sub):
    p = sub.add_parser("trial", help="实盘试行（政策 B）状态、额度与暂停；只读，从不下单、不入金")
    ss = p.add_subparsers(dest="trial_cmd", required=True)
    ss.add_parser("status").set_defaults(func=cmd_status)
    i = ss.add_parser("init"); i.add_argument("--planned", type=float, required=True); i.set_defaults(func=cmd_init)
    fd = ss.add_parser("fund"); fd.add_argument("--amount", type=float, required=True); fd.add_argument("--note", required=True)
    fd.set_defaults(func=cmd_fund)
    r = ss.add_parser("resume"); r.add_argument("--note", required=True); r.set_defaults(func=cmd_resume)
