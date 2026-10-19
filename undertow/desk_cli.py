"""下单台命令：spread-quote（组合单定价）与 sides（买方 vs 卖方同期对比）。只读，从不下单。

用户 2026-10-15：「有些重要的功能或者操作，如果是你手动做的，你应该写成模块代码固定下来。」
计算全在 analyze/spread_pricing.py；这里只负责取数（长桥 depth 经 lb_budget、CBOE 快照、账户只读）与排版。
现价/商品价/位点换算走现有 `undertow quote --commodity --level`（cli.cmd_quote），不另立入口。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from undertow.analyze import risk_policy as rp
from undertow.analyze import spread_pricing as sp

ET = ZoneInfo("America/New_York")


def _et_now() -> datetime:
    return datetime.now(ET)


def resolve(underlying: str):
    """品种 key（wti）或代码（USO / USO.US）→ (key|None, ETF 根代码, Instrument|None)。"""
    from undertow.core.config import load_config
    cfg = load_config()
    inst = cfg.instruments.get(underlying.lower())
    if inst is not None and inst.options and inst.options.symbol:
        return underlying.lower(), inst.options.symbol, inst
    root = underlying.split(".")[0].upper()
    for k, v in cfg.instruments.items():
        if v.options and v.options.symbol == root:
            return k, root, v
    return None, root, None


def commodity_refs(key: str | None, inst) -> list[tuple[str, str]]:
    """主参考（config commodity）+ 额外参考（spread_pricing.EXTRA_COMMODITY_REFS）。"""
    refs = []
    if inst is not None and getattr(inst, "commodity", None) is not None:
        s = inst.commodity.symbol
        refs.append((s, sp.COMMODITY_LABELS.get(s, s)))
    for s, lab in sp.EXTRA_COMMODITY_REFS.get(key or "", []):
        if s not in [r[0] for r in refs]:
            refs.append((s, lab))
    return refs


def fetch_commodity(symbol: str) -> tuple[float, str] | None:
    """Yahoo 实时（不读 6 小时缓存：比值必须与 ETF 价同一时刻）。失败 → None（调用方标 unknown，不阻断）。"""
    from undertow.collect.yahoo_futures import YahooFuturesSource
    try:
        return YahooFuturesSource().fetch_quote(symbol, use_cache=False)
    except Exception:                                    # noqa: BLE001 —— 网络/格式错误都按 unknown 呈现
        return None


def etf_price(root: str) -> tuple[float, str] | None:
    from undertow.collect.longbridge_quote import fetch_stock_quotes, LiveQuotesUnavailable
    try:
        q = fetch_stock_quotes([f"{root}.US"]).get(f"{root}.US")
    except LiveQuotesUnavailable:
        return None
    return (q.freshest, q.freshest_kind) if q and q.freshest > 0 else None


def ratios_for(key, inst, spot) -> list[tuple[str, str, float | None, float | None, str | None]]:
    """[(商品代码, 说明, 商品价, 参考比值, 商品报价时刻)]；取不到 → 价与比值 None。
    叫「参考比值」不叫「实时比值」（Codex 052/054）：不读缓存只保证不拿 6 小时前的旧价，不保证 ETF 与期货两腿同一时刻；
    长桥常规段不带时间戳、Yahoo 时刻有时只到日期，两腿同步无法核实。报价时刻原样交给调用方显示。"""
    out = []
    for s, lab in commodity_refs(key, inst):
        r = fetch_commodity(s)
        px, asof = (r[0], r[1] or None) if r else (None, None)
        out.append((s, lab, px, sp.realtime_ratio(px, spot), asof))
    return out


SYNC_NOTE = "比值为参考：ETF 与期货报价各自取数，两腿是否同一时刻未核实"


def latest_chain(key: str | None, root: str):
    """最新 CBOE 期权快照（昨收口径）→ (快照日, contracts)。没有 → None。"""
    from undertow.collect.store import SnapshotStore
    from undertow.collect.cboe_options import snapshot_from_payload
    got = SnapshotStore().latest("options", root)
    if not got:
        return None
    d, payload = got
    return d, snapshot_from_payload(payload, key or root.lower(), root).contracts


def _find(contracts, expiry: date, kind: str, strike: float):
    return next((c for c in contracts if c.expiry == expiry and c.kind == kind and abs(c.strike - strike) < 1e-6), None)


def legs_from_snapshot(contracts, expiry: date, kind: str, buy_k: float, sell_k: float) -> list[sp.LegQuote] | None:
    out = []
    for k, side in ((buy_k, 1), (sell_k, -1)):
        c = _find(contracts, expiry, kind, k)
        if c is None:
            return None
        out.append(sp.LegQuote(strike=k, kind=kind, side=side, bid=c.bid or None, ask=c.ask or None,
                               bid_size=c.bid_size, ask_size=c.ask_size, delta=c.delta, stale=True))   # 昨收快照 = 陈旧
    return out


def legs_from_depth(root: str, expiry: date, kind: str, buy_k: float, sell_k: float, deltas: dict | None = None):
    """实时 Level-2 买一/卖一（长桥 depth，经 lb_budget）。取数失败的腿 bid/ask=None，并带回错误说明。"""
    from undertow.collect.longbridge_bars import option_symbol
    from undertow.collect.longbridge_quote import fetch_depth, LiveQuotesUnavailable
    codes = {k: option_symbol(root, expiry.isoformat(), kind, k) for k in (buy_k, sell_k)}
    try:
        dep = fetch_depth(list(codes.values()))
    except LiveQuotesUnavailable as e:
        return None, [str(e)[:160]]
    legs, errs = [], []
    for k, side in ((buy_k, 1), (sell_k, -1)):
        d = dep.get(codes[k])
        if d is None or d.error:
            errs.append(f"{codes[k]}：{(d.error if d else '无返回')}")
        legs.append(sp.LegQuote(strike=k, kind=kind, side=side, bid=d.bid if d else None, ask=d.ask if d else None,
                                bid_size=d.bid_size if d else 0, ask_size=d.ask_size if d else 0,
                                delta=(deltas or {}).get(k), error=(d.error if d else "无返回") or ""))
    return legs, errs


def parse_strikes(s: str) -> tuple[float, float]:
    a, b = s.replace("C", "").replace("P", "").replace("c", "").replace("p", "").split("/")
    return float(a), float(b)


def _budget_context():
    """当前账户权益 + 在场账本 + 暂停视图（全部只读；暂停视图 persist=False 不写状态）。
    返回 (capital|None, book|None, pause|None, notes)。任一缺失 → 预算 incomplete（未知不是通过）。"""
    notes = []
    from undertow.collect import longbridge_account as lb
    try:
        from undertow.cli import _load_account_review
        bundle = _load_account_review(False)
        assets = bundle.get("assets")
        if assets is None:
            assets = lb.fetch_assets()
        book = bundle["risk"]["book"] if bundle.get("review") is not None else []
    except Exception as e:                               # noqa: BLE001
        return None, None, None, [f"账户只读失败：{type(e).__name__}: {str(e)[:120]}"]
    if book:                                             # 同标的净额：已有腿用真实现金流水（Codex 061），失败则留成本价口径并说明
        try:
            from undertow.analyze.risk_aggregate import apply_cash_ledger
            since = (date.today() - timedelta(days=90)).isoformat()
            notes += apply_cash_ledger(book, lb.fetch_cash_flow(start=since) or [], since=since)
        except Exception as e:                           # noqa: BLE001
            notes.append(f"现金流水读取失败（{type(e).__name__}）→ 净额用券商成本价口径")
    capital = {"equity": assets.net_assets, "asof": _et_now().isoformat(), "buying_power": assets.buy_power}
    neg = {k: v for k, v in assets.cash_by_ccy.items() if v < 0}
    if neg:
        notes.append("⚠️ 负现金：" + "、".join(f"{k} {v:,.2f}" for k, v in neg.items()) + "（外币负余额是否计息未核实）")
    try:
        from undertow.trial_cli import pause_view
        pause, pnote = pause_view(assets.net_assets, equity_complete=True, asof=capital["asof"], persist=False)
        if pnote:
            notes.append(pnote)
    except Exception as e:                               # noqa: BLE001
        pause = None
        notes.append(f"暂停状态读取失败：{type(e).__name__}")
    return capital, book, pause, notes


def _netting_lines(b: dict, root: str) -> list[str]:
    """同标的整体计险（Codex 061/062）：本标的风险单元 操作前 → 拟建后，及旧口径「各笔相加」上界。"""
    net = b.get("netting")
    if not net:
        return []
    u = rp.unit_key(root)
    pick = lambda xs: next((x for x in xs if x["unit"] == u), None)   # noqa: E731
    bef, aft = pick(net["before"]), pick(net["after"])
    if aft is None:
        return []
    if not aft.get("netted"):
        return [f"  {u} 同标的：{aft.get('note')}"]
    r = aft["risk"]
    ml = "—" if r["max_loss"] is None else f"${r['max_loss']:,.2f}"
    head = (f"  {u} 同标的整体（{r['bound_type']}，{r['method']}）：拟建后最大亏损 {ml}"
            + (f" · 最大收益 ${r['max_gain']:,.2f}" if r.get("max_gain") is not None else "")
            + (f"；操作前 ${bef['risk']['max_loss']:,.2f}" if bef and bef.get("netted") and bef["risk"]["max_loss"] is not None else "")
            + (f"；各笔相加上界 ${aft['gross_upper']:,.2f}（旧口径，只作对照）" if aft.get("gross_upper") is not None else ""))
    out = [head, f"    止损：{aft.get('stop_basis')}" + (f" ${aft['stop_loss']:,.2f}" if aft.get("stop_loss") is not None else ""),
           f"    现金口径：{r['cash_basis']}"]
    if r.get("bound_type") == "joint_bound" and r.get("max_loss") and r.get("max_gain") is not None:
        pf = rp.payoff_floor(r["max_gain"], r["max_loss"])
        out.append(f"    整体收益风险比 {pf['ratio']:.0%}（下限 {rp.PAYOFF_FLOOR['min_net_gain_to_max_loss']:.0%}：{pf['status']}）")
    return out


def _fmt(x, nd=2):
    return "—" if x is None else f"{x:,.{nd}f}"


def _signed_label(net: float | None, debit: bool) -> str:
    if net is None:
        return "不可计算"
    return f"付 {net:.2f}" if net >= 0 else f"收 {-net:.2f}"


def _usd(x) -> str:
    """带符号美元：负收益显示 −$2.20，不出现「+$-2.20」。"""
    return "—" if x is None else (f"+${x:,.2f}" if x >= 0 else f"−${-x:,.2f}")


def _econ_line(e: dict) -> str:
    """每组与整笔分开写（Codex 052 I3：旧版只显示整笔却被预算当每组用）。"""
    one = f"每组含费最大亏损 ${e['unit_max_loss']:,.2f} · 最大收益 {_usd(e['unit_max_gain'])}"
    if e["qty"] > 1:
        one += f"；{e['qty']} 组整笔最大亏损 ${e['total_max_loss']:,.2f} · 最大收益 {_usd(e['total_max_gain'])}"
    return one


def _floor_line(e: dict) -> str:
    """收益风险比 + 两点打平胜率；含费最大收益 ≤ 0 时不报打平胜率（旧版把 None 折成 0.0%）。"""
    pf = e["payoff_floor"]
    ratio = "—" if pf["ratio"] is None else f"{pf['ratio']:.0%}"
    if not e["fee_profit_possible"]:
        return f"  收益风险比 {ratio}（下限 {pf['limit']:.0%}：{pf['status']}）· 含费最大收益 ≤ 0：任何到期价都不盈利，没有打平胜率"
    bw = e["breakeven_win_rate"]
    return (f"  收益风险比 {ratio}（下限 {pf['limit']:.0%}：{pf['status']}）· 理论两点打平胜率 "
            f"{'—' if bw is None else f'{bw * 100:.1f}%'}（赢满/亏满模型，不是实际打平胜率）")


def _pos_int(x: str) -> int:
    import argparse
    v = int(x)
    if v < 1:
        raise argparse.ArgumentTypeError("组数必须是正整数")
    return v


def cmd_spread_quote(args) -> int:
    key, root, inst = resolve(args.underlying)
    expiry = date.fromisoformat(args.expiry)
    kind = args.kind.upper()
    k1, k2 = parse_strikes(args.strikes)
    lo, hi = min(k1, k2), max(k1, k2)
    # 借记：call 买低卖高、put 买高卖低；--credit 反过来。
    buy_k, sell_k = ((lo, hi) if kind == "C" else (hi, lo))
    if args.credit:
        buy_k, sell_k = sell_k, buy_k
    now = _et_now()
    print(f"══ {root} {expiry} {kind} 买 {buy_k:g} / 卖 {sell_k:g} × {args.qty} · 美东 {now:%Y-%m-%d %H:%M} ══")
    snap = latest_chain(key, root)
    deltas = {}
    if snap:
        for k in (buy_k, sell_k):
            c = _find(snap[1], expiry, kind, k)
            if c is not None:
                deltas[k] = c.delta
    if args.snapshot:
        if not snap:
            print("[错误] 无期权快照", file=sys.stderr); return 1
        legs = legs_from_snapshot(snap[1], expiry, kind, buy_k, sell_k)
        if legs is None:
            print("[错误] 快照里没有这两个合约", file=sys.stderr); return 1
        src, errs = f"CBOE 快照 {snap[0]}（昨收口径，标的一动就过时）", []
    else:
        legs, errs = legs_from_depth(root, expiry, kind, buy_k, sell_k, deltas)
        if legs is None:
            print(f"[错误] 实时盘口不可用：{errs[0]}；可加 --snapshot 用昨收快照", file=sys.stderr); return 1
        src = "长桥实时 Level-2 买一/卖一"
    spec = sp.vertical(legs)
    print(f"结构：{spec['name']} · 宽 {spec['width']:g} · 报价来源：{src}")
    for lg in legs:
        print(f"  {'买' if lg.side > 0 else '卖'} {lg.strike:g}{kind}：买价 {_fmt(lg.bid)}（{lg.bid_size}）/ "
              f"卖价 {_fmt(lg.ask)}（{lg.ask_size}）" + (f" · Δ{lg.delta:+.2f}（快照）" if lg.delta is not None else ""))
    for e in errs:
        print(f"  ⚠️ {e}")
    px = sp.net_prices(legs, allow_stale=bool(args.snapshot))      # 显式 --snapshot 才允许按昨收价算（只作参考）
    print(f"\n整单净价（{'借记：付' if spec['debit'] else '贷记：收'}）：逐腿对手价 {_signed_label(px['worst'], spec['debit'])} · "
          f"中价 {_signed_label(px['mid'], spec['debit'])} · 最好 {_signed_label(px['best'], spec['debit'])}")
    print("  逐腿对手价 = 各腿买一/卖一合成的参考，不保证整单立即成交" +
          ("；⚠️ 昨收快照价已陈旧，只作情景参考，不给挂单建议" if px["stale"] else
           "；长桥 depth 不带时间戳，新鲜度未核实"))
    if px["problems"]:
        print("  ⚠️ 盘口不可信（三档全部不可计算，不出价）：" + "；".join(px["problems"]))
    if px["missing"]:
        print("  ⚠️ 缺盘口（对应档不可计算，不折零）：" + "；".join(px["missing"]))
    lr = sp.limit_range(px)
    if lr:
        print(f"建议组合单挂单：起挂 {_signed_label(lr['start'], spec['debit'])}，上限 {_signed_label(lr['cap'], spec['debit'])}"
              f"（{lr['meta']['status']}；{lr['meta']['cap']}）")
    else:
        print("建议挂单区间：不可计算（中价或对手价缺失，或盘口陈旧/不可信）")

    spot_r = etf_price(root)
    ratios = ratios_for(key, inst, spot_r[0]) if spot_r else []
    if spot_r:
        print(f"\n{root} 现价 {spot_r[0]:.2f}（{spot_r[1]}）" + "".join(
            f" · {s} {_fmt(p)}（参考比值 {_fmt(r, 4)}，{s} 时刻 {t or '未知'}）" if p else f" · {s} unknown"
            for s, _, p, r, t in ratios))
        if any(p for _, _, p, _, _ in ratios):
            print(f"  （{SYNC_NOTE}）")

    capital = book = pause = None
    notes = []
    if not args.no_account:
        capital, book, pause, notes = _budget_context()
    rows = [("逐腿对手价", px["worst"]), ("中价", px["mid"])] + ([("挂单上限", lr["cap"])] if lr else [])
    for label, net in rows:
        e = sp.economics(spec, net, qty=args.qty)
        if e is None:
            print(f"\n[{label}] 不可计算"); continue
        conv = "".join(f"；{s} ≈ {_fmt(sp.convert_level(e['breakeven'], r, to_commodity=True))}" for s, _, _, r, _ in ratios if r)
        print(f"\n[{label} {_signed_label(net, spec['debit'])}] {_econ_line(e)} · "
              f"到期盈亏平衡 {root} {e['breakeven']:.2f}{conv}")
        print(_floor_line(e))
        if not args.no_account:
            b = rp.evaluate_budget(candidate=sp.budget_candidate(spec, e, root, f"{root}|{expiry}|{kind}{buy_k:g}/{sell_k:g}",
                                                                 expiry=expiry),
                                   open_positions=book, capital=capital, pause=pause)
            feas = b.get("feasible_units") or []
            print(f"  政策 B 预算（请求 {args.qty} 组）：{b['budget_status']}（可开 {b['allowed_units']} 组"
                  + (f"；可行组数 {'、'.join(map(str, feas))}" if feas and feas != list(range(1, b['allowed_units'] + 1)) else "")
                  + "）" + ("" if b["budget_status"] == "pass" else " —— " + "；".join(b["reasons"][:3])))
            for line in _netting_lines(b, root):
                print(line)
    if not args.no_account and capital is not None:
        def ok(e):
            b = rp.evaluate_budget(candidate=sp.budget_candidate(spec, e, root, "bound", expiry=expiry), open_positions=book,
                                   capital=capital, pause=pause)
            return b["allowed_units"] >= args.qty
        pb = sp.price_bound_under(spec, ok, qty=args.qty)
        fb = sp.price_bound_under(spec, lambda e: e["payoff_floor"]["status"] == "pass", qty=args.qty)
        what = "最高可付" if spec["debit"] else "最少需收"

        def bound(x):
            return "无可行净价（(0, 宽度) 内任何净价都不通过）" if x is None else _signed_label(x, spec["debit"])
        print(f"\n政策 B 下{what}（{args.qty} 组）：{bound(pb)} · 收益风险比下限下{what}：{bound(fb)}")
    for n in notes:
        print(n)
    print(f"\n处置：{sp.ASSIGNMENT_NOTE}")
    from undertow.analyze.net_risk import EXPIRY_NOTE
    print(f"到期处置：{EXPIRY_NOTE}")
    print(f"出场：{sp.BROKER_NOTES['combo_leg_exit']}")
    print("⚠️ 只读参考，不是下单指令；价格是取数这一刻的盘口，挂单前在券商端核对；价差权限与组合占用未核实。")
    return 0


def cmd_sides(args) -> int:
    key, root, inst = resolve(args.underlying)
    horizon = date.fromisoformat(args.horizon)
    today = _et_now().date()
    snap = latest_chain(key, root)
    if not snap:
        print("[错误] 无期权快照", file=sys.stderr); return 1
    d_snap, contracts = snap
    R = args.equity if args.equity is not None else rp.POLICY_B["planned_trial_capital_usd"]
    cat = rp.category_of(root if key is None else key)
    cat_lim = rp.limit_usd(rp.POLICY_B["categories"][cat], R)
    print(f"══ {root} 买方 vs 卖方 · 到 {horizon}（{(horizon - today).days} 天）· 报价 {'实时 depth' if args.live else f'CBOE 快照 {d_snap}（昨收）'} ══")
    print(f"R = ${R:,.2f}{'（--equity）' if args.equity is not None else '（计划资本，未读账户）'} · {cat} 类单笔上限 ${cat_lim:,.0f}")

    def legs_for(exp, kind, buy_k, sell_k):
        if args.live:
            dl = {k: (c.delta if (c := _find(contracts, exp, kind, k)) else None) for k in (buy_k, sell_k)}
            legs, _ = legs_from_depth(root, exp, kind, buy_k, sell_k, dl)
            return legs
        return legs_from_snapshot(contracts, exp, kind, buy_k, sell_k)

    def report(label, exp, kind, buy_k, sell_k, rounds=1):
        legs = legs_for(exp, kind, buy_k, sell_k)
        if legs is None:
            print(f"\n{label}：合约不在快照里"); return
        spec = sp.vertical(legs)
        px = sp.net_prices(legs, allow_stale=not args.live)        # 快照模式本就是昨收情景，显式允许并标注
        lng = next(l for l in legs if l.side > 0); sht = next(l for l in legs if l.side < 0)
        print(f"\n{label}：{exp} 买 {buy_k:g}{kind} / 卖 {sell_k:g}{kind}（{spec['name']}，DTE {(exp - today).days}）")
        if px["problems"]:
            print("  ⚠️ 盘口不可信（不出价）：" + "；".join(px["problems"]))
        mid_e = None
        for nm, net in (("逐腿对手价", px["worst"]), ("中价", px["mid"])):
            e = sp.economics(spec, net)
            if e is None:
                print(f"  [{nm}] 不可计算"); continue
            mid_e = e if nm == "中价" else (mid_e or e)
            gain_round, loss = e["unit_max_gain"], e["unit_max_loss"]
            fits = "✅" if loss <= cat_lim else "❌"
            bw = e["breakeven_win_rate"]
            line = (f"  [{nm} {_signed_label(net, spec['debit'])}] 每轮含费最大收益 {_usd(gain_round)} / 最大亏损 −${loss:,.2f} · "
                    + (f"两点打平胜率 {bw * 100:.0f}%" if bw is not None else "含费无盈利可能")
                    + f" · 收益风险比 {e['payoff_floor']['ratio']:.0%}（下限 {e['payoff_floor']['status']}）· {cat} 类上限 {fits}")
            if rounds > 1 and gain_round > 0:
                line += (f" · 情景：{rounds} 轮全赢 +${gain_round * rounds:,.0f}（不是期望、不是回测），"
                         f"输一轮抵 {loss / gain_round:.1f} 轮")
            print(line)
        ip = sp.itm_proxy(spec, lng.delta, sht.delta, mid_e)
        if ip:
            print(f"  {ip['text']}")

    if args.buy:
        kb = args.buy[-1].upper()
        b1, b2 = parse_strikes(args.buy)
        report("买方（持有一个到期）", horizon, kb, b1, b2)
    if args.sell:
        ks = args.sell[-1].upper()
        s1, s2 = parse_strikes(args.sell)          # 卖方写法：先写卖出的行权价，再写买入保护的
        exps = sorted({c.expiry for c in contracts if c.expiry > today and c.expiry <= horizon})
        if not exps:
            print("\n卖方：horizon 内没有到期"); return 0
        exp = min(exps, key=lambda e: (abs((e - today).days - args.round_dte), e))
        rounds = sp.roll_plan((exp - today).days, (horizon - today).days)
        report(f"卖方（按 {(exp - today).days} 天一轮滚动，约 {rounds} 轮；后续各轮按今天同 DTE 报价近似）",
               exp, ks, s2, s1, rounds=max(rounds, 1))
    print("\n说明：「N 轮全赢」只是情景，不是策略期望、也不是回测 —— 后续各轮按今天同 DTE 报价近似，"
          "真实各轮的方向、权利金、空窗、止损与资金能否复用都未知；轮数由 --round-dte 固定给出，不按当前报价优化。"
          "滚动多轮不改变每轮期望，只增加次数；每轮手续费与点差都会重付。卖方要靠方向判断/墙位把胜率抬过打平线。")
    print(f"处置：{sp.ASSIGNMENT_NOTE}")
    print(f"费用按 spread_econ 往返模型：${sp.FEE_PER_CONTRACT}/张/笔 × 2 腿 × 2 笔 = ${4 * sp.FEE_PER_CONTRACT:.2f}/组（指派处置费用未知）。")
    return 0


def _parse_delta_leg(x: str) -> dict:
    """「带符号张数:C|P:行权价:到期」，如 -2:P:50:2026-10-30（负 = 卖出/回补空头的反向是正）。"""
    import argparse
    try:
        q, k, st, e = x.split(":")
        q, k, st = int(q), k.upper(), float(st)
        date.fromisoformat(e)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"腿格式应为 带符号张数:C|P:行权价:YYYY-MM-DD，收到 {x!r}") from err
    if q == 0 or k not in ("C", "P") or st <= 0:
        raise argparse.ArgumentTypeError(f"腿不合法：{x!r}")
    return {"kind": k, "strike": st, "expiry": e, "qty": q}


def _delta_cash_from_depth(root: str, legs: list[dict]) -> tuple[float | None, list[str]]:
    """拟操作的现金情景：买入按 ask、卖出按 bid（逐腿对手价，不保证成交）减开仓费模型。缺一侧 → None（不折零）。"""
    from undertow.collect.longbridge_bars import option_symbol
    from undertow.collect.longbridge_quote import fetch_depth, LiveQuotesUnavailable
    codes = [option_symbol(root, lg["expiry"], lg["kind"], lg["strike"]) for lg in legs]
    try:
        dep = fetch_depth(codes)
    except LiveQuotesUnavailable as e:
        return None, [f"实时盘口不可用：{str(e)[:120]}"]
    cash, lines = 0.0, []
    for lg, c in zip(legs, codes):
        d = dep.get(c)
        px = None if d is None or d.error else (d.ask if lg["qty"] > 0 else d.bid)
        size = None if d is None else (d.ask_size if lg["qty"] > 0 else d.bid_size)
        if not (isinstance(px, (int, float)) and px > 0) or size == 0:
            return None, [f"{c} 缺{'卖一' if lg['qty'] > 0 else '买一'} → 现金情景不可计算"]
        cash += -lg["qty"] * sp.MULT * px - abs(lg["qty"]) * sp.FEE_PER_CONTRACT
        lines.append(f"{'买' if lg['qty'] > 0 else '卖'} {abs(lg['qty'])} 张 {c} @ {px:.2f}")
    return round(cash, 2), lines


def cmd_risk_unit(args) -> int:
    """同标的整体风险：现仓 →（可选）拟加仓/减仓/部分平仓后的剩余组合（Codex 061/062；只读，从不下单）。"""
    from undertow.analyze import net_risk as nr
    key, root, inst = resolve(args.underlying)
    u = rp.unit_key(root)
    now = _et_now()
    print(f"══ {u} 同标的整体风险 · 美东 {now:%Y-%m-%d %H:%M} · {nr.VERSION} ══")
    capital, book, pause, notes = _budget_context()
    if book is None:
        print("账户只读失败 → 无法计算：" + "；".join(notes)); return 1
    rows = [b for b in book if rp.unit_key(b.get("underlying")) == u]
    if any(b.get("legs") is None for b in rows):
        print("同标的有缺腿明细的持仓 → 不能净额，只能按各笔相加（保守上界）：" + "、".join(b["id"] for b in rows))
        return 1
    cur_legs = [lg for b in rows for lg in b["legs"]]
    cur_cash = round(sum(b["net_cash"] for b in rows), 2) if rows else 0.0
    delta = args.leg or []
    dcash, dlines = (args.cash, [f"现金情景由 --cash 给出：{args.cash:+.2f}"]) if args.cash is not None else (
        _delta_cash_from_depth(root, delta) if delta else (0.0, []))
    w = nr.what_if(cur_legs, cur_cash, delta, dcash if dcash is not None else float("nan"),
                   future_fee_per_contract=sp.FEE_PER_CONTRACT,
                   cash_basis="；".join(sorted({b.get("cash_basis") or "未注明" for b in rows})) or "空仓")

    def show(r, title):
        legs = "、".join(f"{lg['qty']:+g} {lg['strike']:g}{lg['kind']} {lg['expiry']}" if lg["kind"] != "STOCK"
                        else f"{lg['qty']:+g} 股" for lg in r["net_legs"]) or "（无）"
        ml = "—" if r["max_loss"] is None else f"${r['max_loss']:,.2f}"
        mg = "未建模" if r["max_gain"] is None and not r["max_gain_unbounded"] else (
            "无上限" if r["max_gain_unbounded"] else f"${r['max_gain']:,.2f}")
        print(f"\n[{title}] {legs}")
        print(f"  风险界 {r['bound_type']}（{r['method']}）：最大亏损 {ml}" + (f"（S={r['worst_at']:g}）" if r.get("worst_at") is not None else "")
              + f" · 最大收益 {mg} · 净现金流 {r['net_cash'] if r['net_cash'] is not None else '—'} · 未来费用 {r['future_fees']}")
        sb = nr.stop_bound(r)
        print(f"  止损：" + (f"整组价值非负 → 止损损失 ≤ ${sb:,.2f}（上界，非止损规则）" if sb is not None else "整组止损规则待补"))
        for a in r["assumptions"]:
            print(f"  · {a}")
        for pr in r["problems"]:
            print(f"  ⚠️ {pr}")
    show(w["before"], "现仓")
    if delta:
        print("\n拟操作：" + "；".join(dlines))
        show(w["after"], "拟操作后（情景，未成交保护不计入当前）")
        if w["delta_max_loss"] is not None:
            print(f"\n最大亏损变化：{w['delta_max_loss']:+,.2f}" + ("（风险增加：减仓/平保护腿不一定减险）" if w["delta_max_loss"] > 0 else ""))
    if capital is not None and dcash is not None:
        after_book = [b for b in book if rp.unit_key(b.get("underlying")) != u]
        if w["after"]["net_legs"]:
            after_book.append({"id": f"{u}·拟操作后", "underlying": root, "stop_loss": None, "payoff_max_loss": None,
                               "legs": w["after"]["net_legs"], "net_cash": cur_cash + dcash,
                               "future_fees": w["after"]["future_fees"], "cash_basis": "拟操作后情景"})
        b = rp.evaluate_book(after_book, capital)
        print(f"\n政策 B 整本账（{'拟操作后' if delta else '现仓'}）：{b['overall']}")
        for c in b["checks"]:
            if c["status"] != "pass" or u in c["check"] or "合计" in c["check"] or "大仓" in c["check"]:
                v = c["value"]
                print(f"  [{c['status']}] {c['check']}：{'—' if v is None else v} / 上限 {c['limit']}" + (f"（{c['note']}）" if c.get("note") else ""))
    for n in notes:
        print(n)
    print(f"\n到期处置：{nr.EXPIRY_NOTE}")
    print("⚠️ 只读计算，不是下单指令；拟操作是报价情景，部分成交后按真实持仓重跑本命令。")
    return 0


def register(sub):
    p = sub.add_parser("spread-quote", help="垂直价差组合单定价：净价三档、挂单区间、含费盈亏、政策 B 预算（只读）")
    p.add_argument("underlying", help="品种 key 或代码，如 wti / USO")
    p.add_argument("expiry", help="到期 YYYY-MM-DD")
    p.add_argument("kind", choices=("C", "P", "c", "p"))
    p.add_argument("strikes", help="两个行权价，如 150/155")
    p.add_argument("--credit", action="store_true", help="贷记价差（默认借记：call 买低卖高、put 买高卖低）")
    p.add_argument("--qty", type=_pos_int, default=1, help="组数（预算与价格边界都按这个组数算）")
    p.add_argument("--snapshot", action="store_true", help="用 CBOE 昨收快照代替实时盘口")
    p.add_argument("--no-account", action="store_true", help="不读账户（跳过政策 B 预算）")
    p.set_defaults(func=cmd_spread_quote)
    r = sub.add_parser("risk-unit", help="同标的整体风险：现仓与拟加仓/减仓/部分平仓后的剩余组合（只读）")
    r.add_argument("underlying", help="品种 key 或代码，如 silver / SLV")
    r.add_argument("--leg", action="append", type=_parse_delta_leg,
                   help="拟变更腿，可多次：带符号张数:C|P:行权价:到期，如 -2:P:50:2026-10-30（平仓 = 反向数量）")
    r.add_argument("--cash", type=float, help="拟操作的现金情景（收为正、已扣费）；缺省按长桥买一/卖一逐腿对手价")
    r.set_defaults(func=cmd_risk_unit)
    s = sub.add_parser("sides", help="买方 vs 卖方同期对比（卖方按短到期滚动多轮）（只读）")
    s.add_argument("underlying")
    s.add_argument("--horizon", required=True, help="比较到哪天，YYYY-MM-DD（买方用这个到期）")
    s.add_argument("--buy", help="买方借记价差，先写买入行权价，如 150/155C")
    s.add_argument("--sell", help="卖方贷记价差，先写卖出行权价，如 148/147P")
    s.add_argument("--round-dte", type=int, default=7, help="卖方每轮目标天数（选最接近的到期）")
    s.add_argument("--equity", type=float, help="R（默认计划资本 $500）")
    s.add_argument("--live", action="store_true", help="用长桥实时盘口（delta 仍取快照）")
    s.set_defaults(func=cmd_sides)
