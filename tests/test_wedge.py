"""下降楔形识别（wedge-explore-v1）：合成楔形能认出、上破判定、发散/上行通道不认、认出前已破单列、安慰剂可复现。全部合成数据。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import price_structure as ps  # noqa: E402
from undertow.analyze import wedge as wg  # noqa: E402


def mk(closes, spread=0.15, start=date(2000, 1, 3)):
    out, d, prev = [], start, closes[0]
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
        d += timedelta(days=1)
    return out


def zig(nodes, n=6):
    c = []
    for a, b in zip(nodes[:-1], nodes[1:]):
        c += [a + (b - a) * k / n for k in range(1, n + 1)]
    return c


# 上轨 110→100（每段 −2.5），下轨 100→96（每段 −1），收敛；交替贴两线
WEDGE = [110, 100, 107.5, 99, 105, 98, 102.5, 97, 100]


def series(tail):
    pre = [112.0] * 30
    return pre + zig([112] + WEDGE) + tail


def test_detects_falling_wedge_and_upward_break():
    bars = mk(series([100 + 0.8 * k for k in range(1, 40)] + [130.0] * 140))
    atr = ps.wilder_atr(bars)
    found = wg.find(bars, atr, "descriptive")
    ups = [(w, d, t) for w, d, t in found if d == "up"]
    assert ups, found
    w, d, t = ups[0]
    assert w.touches >= 5 and w.upper(t) < bars[t][4]
    assert wg.outcome(bars, w, d, t)["complete"]


def test_rejects_rising_channel():
    bars = mk([100.0] * 30 + zig([100, 90, 97.5, 91, 95, 92, 93.5, 93, 92.5]) + [92.0] * 150)   # 低点抬高 → 下轨向上
    atr = ps.wilder_atr(bars)
    assert all(d != "up" or w.l2[1] < w.l1[1] for w, d, t in wg.find(bars, atr, "descriptive"))


def test_causal_marks_break_before_known():
    # 最后一个低点之后立刻大涨：k=3 确认前就越过上轨
    bars = mk(series([104, 112, 120] + [120.0] * 150))
    atr = ps.wilder_atr(bars)
    found = wg.find(bars, atr, "causal")
    assert found
    for w, d, t in found:                      # 可交易口径：突破只能出现在认出（最后摆动点确认）之后
        assert d == "broke_before_known" or t is None or t > w.known
        assert w.known >= w.last + wg.K


def test_check_requires_convergence():
    bars = mk([100.0] * 120)
    atr = [1.0] * len(bars)
    P = ps.FPivot
    run = [P("H", 20, 23, 110), P("L", 26, 29, 100), P("H", 32, 35, 108), P("L", 38, 41, 96), P("H", 44, 47, 106)]
    # 上轨每根 −1/6，下轨每根 −1/3：下轨更陡 → 发散，不成立
    assert wg.check(run, bars, atr) is None


def test_placebo_reproducible():
    bars = mk(series([100 + 0.3 * k for k in range(1, 400)]))
    atr = ps.wilder_atr(bars)
    found = [x for x in wg.find(bars, atr, "descriptive") if x[1] in ("up", "down")]
    if not found:
        return
    w, d, t = found[0]
    a = wg.placebo_up_share(bars, atr, w, w.last + 1, seed=7, n=10)
    b = wg.placebo_up_share(bars, atr, w, w.last + 1, seed=7, n=10)
    assert a == b


# ================= v1.1 causal_stream：前缀一致性（Codex 053 C 包；全部合成数据） =================
import math  # noqa: E402
import random  # noqa: E402


def codex053(n=100, overrides=None, lows=None, closes=None):
    """Codex 053 review_probe.py 的合成反例（原样复制）：前 55 根 first9/last50/known53、第 54 根上破；
    第 60 根 109 的高点（确认于 63）在全历史最终投影里取代 H50。overrides/lows/closes 只供变体用。"""
    hi_spikes = dict(((10, 110), (30, 108), (50, 106), (54, 107.1), (60, 109)))
    lo_spikes = dict(((20, 100), (40, 99)))
    if overrides is not None:
        hi_spikes = overrides
    if lows is not None:
        lo_spikes = lows
    bars = []
    for t in range(n):
        upper = 111 - .1 * t
        lower = 101 - .05 * t
        c = (upper + lower) / 2
        if t < 10:
            c = 109 + t / 10
        h, lo = c + .1, c - .1
        if t in hi_spikes:
            h = hi_spikes[t]
        if t in lo_spikes:
            lo = lo_spikes[t]
        if overrides is None and t == 54:
            c = 107
            h = 107.1
        if closes and t in closes:
            c = closes[t]
            h, lo = max(h, c), min(lo, c)
        bars.append((date(2000, 1, 1) + timedelta(days=t), c, h, lo, c))
    return bars


def ident(e):
    w = e.wedge
    return (w.first, w.last, w.known, w.h1, w.h2, w.l1, w.l2, w.touches, w.apex, w.pivots)


def run(bars, atr=None):
    return wg.stream(bars, ps.wilder_atr(bars) if atr is None else atr)


def assert_prefix_consistent(bars, cuts, flat_atr=False):
    """对每个截断长度 cut：前 cut 根跑出的事件日志 == 全数据日志里根 < cut 的部分；
    认出根 < cut 的事件身份、轨道一致；定性根 < cut 的定性一致，否则前缀里仍未定性。"""
    full_atr = [1.0] * len(bars) if flat_atr else None
    full, flog = run(bars, full_atr)
    for cut in cuts:
        pre, plog = run(bars[:cut], None if full_atr is None else full_atr[:cut])
        assert plog == [x for x in flog if x[0] < cut], cut
        want = [e for e in full if e.wedge.known < cut]
        assert [ident(e) for e in pre] == [ident(e) for e in want], cut
        for p, f in zip(pre, want):
            if f.resolved_at is not None and f.resolved_at < cut:
                assert (p.direction, p.t, p.resolved_at) == (f.direction, f.t, f.resolved_at), cut
            else:
                assert p.direction is None and p.t is None, cut
    return full


def test_atr_and_pivots_are_prefix_stable():
    """流式口径的前提：Wilder ATR 与分形确认点在前缀上与全数据的对应部分相同。"""
    bars = codex053()
    full_piv = ps.fractal_pivots(bars, wg.K)
    full_atr = ps.wilder_atr(bars)
    for cut in range(1, len(bars) + 1):
        assert ps.fractal_pivots(bars[:cut], wg.K) == [p for p in full_piv if p.conf < cut]
        assert ps.wilder_atr(bars[:cut]) == full_atr[:cut]


def test_alt_push_matches_alternating_stepwise():
    rng = random.Random(53)
    c, bars = 100.0, []
    for i in range(800):
        o = c
        c *= math.exp(rng.gauss(0, .015))
        h = round(max(o, c) * (1 + abs(rng.gauss(0, .005))), 1)     # 取整制造重复极值
        lo = round(min(o, c) * (1 - abs(rng.gauss(0, .005))), 1)
        bars.append((date(2000, 1, 1) + timedelta(days=i), o, h, lo, c))
    piv = sorted(ps.fractal_pivots(bars, 3), key=lambda p: (p.conf, p.idx, p.kind))
    seq = []
    for j, p in enumerate(piv):
        wg._alt_push(seq, p)
        assert seq == ps.alternating(piv[:j + 1])


def test_codex053_counterexample_event_survives_future_bars():
    bars = codex053()
    atr = [1.0] * len(bars)
    # v1 causal 的缺陷仍可复现（只为说明 v1 为何作废，不改 v1）
    assert [(w.first, w.last, w.known, d, t) for w, d, t in wg.find(bars[:55], atr[:55], "causal")] == [(9, 50, 53, "up", 54)]
    assert wg.find(bars, atr, "causal") == []
    # 全历史最终投影里 H50 已被 H60 取代
    assert [(p.kind, p.idx) for p in ps.alternating(ps.fractal_pivots(bars, 3))][-1] == ("H", 60)
    # v1.1：55 根与 100 根给出同一事件（身份、轨道、认出根、方向、突破根）
    a, alog = wg.stream(bars[:55], atr[:55])
    b, blog = wg.stream(bars, atr)
    assert [(e.wedge.first, e.wedge.last, e.wedge.known, e.direction, e.t) for e in a] == [(9, 50, 53, "up", 54)]
    assert [ident(e) for e in a] == [ident(e) for e in b[:1]]
    assert (b[0].direction, b[0].t) == ("up", 54)
    assert b[0].wedge.pivots[-1][:3] == ("H", 50, 53)
    assert blog[:len(alog)] == alog
    assert [(w.first, w.known, d, t) for w, d, t in wg.find(bars, atr, "causal_stream")][:1] == [(9, 53, "up", 54)]
    assert_prefix_consistent(bars, range(5, len(bars) + 1), flat_atr=True)
    assert_prefix_consistent(bars, range(20, len(bars) + 1))


def test_immature_and_recognized_unbroken_states():
    bars = codex053()
    atr = [1.0] * len(bars)
    for cut in (40, 50, 53):                      # H50 尚未确认（确认根 53 收盘后）→ 没有事件
        assert wg.stream(bars[:cut], atr[:cut])[0] == []
    ev, _ = wg.stream(bars[:54], atr[:54])        # 第 53 根认出、第 54 根尚未发生 → 已认出、未定性
    assert [(e.wedge.known, e.direction) for e in ev] == [(53, None)]
    assert wg.find(bars[:54], atr[:54], "causal_stream")[0][1:] == ("incomplete", None)


def test_broke_before_known_is_frozen():
    # 第 52 根收盘 105.9 已在上轨（≈105.8）之上、但不高于 H50=106（H50 仍是分形高点，要到 53 才确认）
    # → 认出时已破，单列，不再等突破
    bars = codex053(n=140, closes={52: 105.9})
    atr = [1.0] * len(bars)
    ev, log = wg.stream(bars, atr)
    e = ev[0]
    assert (e.wedge.known, e.direction, e.t, e.resolved_at, e.pre_break) == (53, "broke_before_known", None, 53, (52, "up"))
    assert all(x[2] == 0 for x in log[:2]) and log[1] == (53, "resolve", 0, "broke_before_known", None)
    assert_prefix_consistent(bars, range(30, len(bars) + 1), flat_atr=True)


def test_same_kind_supersession_only_with_information_then_available():
    # L40=99 之后 L44=98.7 更低（确认于 47）在 H50 确认前取代 L40；认出时下轨用 L44，而不是事后的点
    hi = {10: 110, 30: 108, 50: 106}
    bars = codex053(n=220, overrides=hi, lows={20: 100, 40: 99, 44: 98.7})
    atr = [1.0] * len(bars)
    ev, _ = wg.stream(bars, atr)
    e = ev[0]
    assert e.wedge.l2 == (44, 98.7) and e.wedge.known == 53
    assert [p[:2] for p in e.wedge.pivots] == [("H", 9), ("L", 20), ("H", 30), ("L", 44), ("H", 50)]
    assert_prefix_consistent(bars, range(30, len(bars) + 1), flat_atr=True)


def test_no_break_until_deadline_and_equal_extreme_does_not_supersede():
    # 认出后一直在两轨之间：到 min(交点, 认出+1+MAX_WAIT) 定为 no_break；
    # 第 70 根高点与 H50 等价（106）：交替投影只让【更】极端的同类点取代，等价点被丢弃，冻结轨道不受影响
    hi = {10: 110, 30: 108, 50: 106}
    bars = codex053(n=200, overrides=hi)
    bars[70] = (bars[70][0], bars[70][1], 106.0, bars[70][3], bars[70][4])
    atr = [1.0] * len(bars)
    ev, _ = wg.stream(bars, atr)
    e = ev[0]
    w = e.wedge
    assert e.direction == "no_break" and e.t is None
    assert e.resolved_at == min(math.floor(w.apex), w.known + 1 + wg.MAX_WAIT)
    assert w.h2 == (50, 106)
    assert_prefix_consistent(bars, range(30, len(bars) + 1), flat_atr=True)


def test_tail_perturbation_never_changes_issued_events():
    base = codex053(n=58)
    for seed in range(12):
        rng = random.Random(seed)
        bars = list(base)
        c = bars[-1][4]
        for i in range(150):
            o = c
            c = c + rng.gauss(0, 1.2)
            h = max(o, c) + abs(rng.gauss(0, .5)) + (5 if rng.random() < .05 else 0)   # 偶尔插入很极端的高点
            lo = min(o, c) - abs(rng.gauss(0, .5)) - (5 if rng.random() < .05 else 0)
            bars.append((bars[-1][0] + timedelta(days=1), o, h, lo, c))
        atr = [1.0] * len(bars)
        ev, log = wg.stream(bars, atr)
        e0, log0 = wg.stream(base, atr[:len(base)])
        assert [ident(e) for e in ev[:len(e0)]] == [ident(e) for e in e0]
        assert (ev[0].direction, ev[0].t) == ("up", 54)
        assert [x for x in log if x[0] < len(base)] == log0


def test_prefix_consistency_random_walks_with_ties():
    for seed in (1, 2, 3):
        rng = random.Random(seed)
        c, bars = 100.0, []
        for i in range(1200):
            o = c
            c *= math.exp(rng.gauss(0, .015))
            h = round(max(o, c) * (1 + abs(rng.gauss(0, .006))), 1)
            lo = round(min(o, c) * (1 - abs(rng.gauss(0, .006))), 1)
            bars.append((date(1990, 1, 1) + timedelta(days=i), o, h, lo, c))
        full, _ = run(bars)
        assert full, seed                              # 合成路径必须真的产生事件，测试才有意义
        cuts = set(range(20, len(bars) + 1, 23))
        for e in full:
            for x in (e.wedge.known, e.wedge.known + 1, e.resolved_at, e.resolved_at and e.resolved_at + 1):
                if x and 0 < x <= len(bars):
                    cuts.add(x)
        assert_prefix_consistent(bars, sorted(cuts))


def test_resolution_is_write_once():
    bars = codex053()
    ev, log = wg.stream(bars, [1.0] * len(bars))
    try:
        ev[0].resolve("down", 70, 70, log)
    except RuntimeError:
        pass
    else:
        raise AssertionError("已定性的事件被改写")
    assert ev[0].direction == "up"


def test_stream_agrees_with_v1_on_single_clean_wedge():
    bars = mk(series([104, 112, 120] + [120.0] * 150))
    atr = ps.wilder_atr(bars)
    v1 = [(w.first, w.last, w.known, d, t) for w, d, t in wg.find(bars, atr, "causal")]
    v11 = [(w.first, w.last, w.known, d, t) for w, d, t in wg.find(bars, atr, "causal_stream")]
    assert v1 == v11


def test_prefix_audit_passes_stream_and_catches_lookahead():
    bars = codex053()                       # 反例用平 ATR=1（与 review_probe 相同）；默认 Wilder ATR 另跑一遍
    flat = lambda b: wg.stream(b, [1.0] * len(b))  # noqa: E731
    assert wg.prefix_audit(bars, range(10, len(bars) + 1), runner=flat) == []
    assert wg.prefix_audit(bars, range(10, len(bars) + 1)) == []
    full, _ = flat(bars)
    assert wg.audit_cuts(full, len(bars), stride=40) == sorted({40, 80, 53, 54, 55})

    def lookahead(b):                       # 模拟 v1 式缺陷：数据一长就把已发事件抹掉
        ev, log = flat(b)
        return ([], []) if len(b) > 60 else (ev, log)
    assert wg.prefix_audit(bars, [55, 100], runner=lookahead) == [55]


def _rw(seed, n=1500):
    rng = random.Random(seed)
    c, bars = 100.0, []
    for i in range(n):
        o = c
        c *= math.exp(rng.gauss(0, .015))
        bars.append((date(1990, 1, 1) + timedelta(days=i), o, max(o, c) * (1 + abs(rng.gauss(0, .006))),
                     min(o, c) * (1 - abs(rng.gauss(0, .006))), c))
    return bars


def test_explore_script_v11_on_synthetic(tmp_path, monkeypatch):
    """--spec v1.1 端到端（合成日线、临时目录；不读设计集）：分母分列、推断移入 inference_uncalibrated、前缀核对与事件变化计数入报告。"""
    import json
    from scripts import wedge_explore as we
    data = {"AAA": _rw(11), "BBB": _rw(12)}
    monkeypatch.setattr(we, "load", lambda s: data[s])
    monkeypatch.setattr(we, "STOCK", ("AAA",))
    monkeypatch.setattr(we, "ETF", ("BBB",))
    monkeypatch.setattr(we, "VARIANTS", {"main": {"k": 3, "tol": 0.3}})
    monkeypatch.setattr(we, "BASE", tmp_path)
    monkeypatch.setattr(sys, "argv", ["wedge_explore.py", "--spec", "v1.1", "--run-id", "t1"])
    assert we.main() == 0
    res = json.loads((tmp_path / "t1/result.json").read_text())
    assert res["modes"] == ["descriptive", "causal_stream"] and res["spec_version"] == "v1.1"
    assert res["prefix_audit"]["main"]["mismatches"] == 0 and res["prefix_audit"]["main"]["cuts"] > 0
    im = res["impact"]["main"]
    assert im["stream_events"] == sum(len(r) for r in res["results"]["main"]["causal_stream"]["rows"].values())
    for mode in res["modes"]:
        s = res["results"]["main"][mode]["groups"]["全部"]
        assert "pb_z" not in s and "pb_p" not in s
        assert s["inference_uncalibrated"]["status"] == we.INFER_NOTE
        assert s["detected"] == s["broke_before_known"] + s["incomplete"] + s["no_break"] + s["up"] + s["down"]
        assert s["direction_n"] == s["up"] + s["down"] >= s["paired_n"]
    st = [r for rs in res["results"]["main"]["causal_stream"]["rows"].values() for r in rs]
    assert st and all("rails" in r and r["pivots"] for r in st)
    assert all("rails" not in r for rs in res["results"]["main"]["descriptive"]["rows"].values() for r in rs)
    md = (tmp_path / "t1/report.md").read_text()
    assert we.INFER_NOTE in md and "前缀重放核对" in md and "认出前已破" in md and "有方向" in md
    monkeypatch.setattr(sys, "argv", ["wedge_explore.py", "--spec", "v1.1", "--run-id", "t1"])
    try:
        we.main()
    except SystemExit as e:
        assert "拒绝覆盖" in str(e)
    else:
        raise AssertionError("run 目录已存在却被覆盖")


def test_explore_script_aborts_on_prefix_mismatch(tmp_path, monkeypatch):
    from scripts import wedge_explore as we
    monkeypatch.setattr(we, "load", lambda s: _rw(11))
    monkeypatch.setattr(we, "STOCK", ("AAA",))
    monkeypatch.setattr(we, "ETF", ())
    monkeypatch.setattr(we, "VARIANTS", {"main": {"k": 3, "tol": 0.3}})
    monkeypatch.setattr(we, "BASE", tmp_path)
    monkeypatch.setattr(we.wg, "prefix_audit", lambda *a, **k: [123])
    monkeypatch.setattr(sys, "argv", ["wedge_explore.py", "--spec", "v1.1", "--run-id", "bad"])
    try:
        we.main()
    except SystemExit as e:
        assert "前缀重放" in str(e)
    else:
        raise AssertionError("前缀不一致却出了结果")
    assert not (tmp_path / "bad").exists()
