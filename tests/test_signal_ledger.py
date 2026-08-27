"""强信号台账与 probe 的确定性测试。

台账要守住的四件事：
  1. **只记事实，不落判断** —— 被闸门拦下的候选必须一样入账，否则永远没有反事实样本，
     闸门阈值只能靠讲故事去调（2026-08-27 差点因此把全集最准的信号掐掉）。
  2. **绝不用现在冒充未来** —— 前瞻窗口没走完的格子必须留 null，不许拿当前价顶。
  3. **样本不够就说不够** —— summarize 的 conclusive 必须同时过 n≥MIN_N 和 p<0.05。
  4. **单一实现** —— probe 记下的数必须和告警用的数逐一相等，否则攒的样本对不上。
"""
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze import signal_ledger as sl
from undertow.analyze.flow import (FlowChange, STRONG_CONTRA_SPOT,
                                   probe_strong_signal, wing_weights)


def _fc(kind, bias, weight, delta, d_oi=5000, strike=100.0):
    return FlowChange(expiry=date(2026, 9, 18), strike=strike, kind=kind,
                      prev_oi=1000, curr_oi=1000 + d_oi, d_oi=d_oi, delta=delta,
                      prev_iv=0.25, curr_iv=0.28, d_iv_pp=3.0, adj_iv_pp=3.19,
                      curr_volume=9000, moneyness=0.0, bias=bias,
                      judgment="x", on_wall="", note="", weight=weight)


@dataclass
class _Vol:
    prev: object = "y"
    d_spot_pct: float = 0.0
    d_atm_pp: float = 0.0
    d_skew25_pp: float = 0.0


@dataclass
class _FA:
    changes: list = field(default_factory=list)
    upside_pressure: float = 0.0
    downside_pressure: float = 0.0
    net_call_doi: int = 0
    net_put_doi: int = 0
    vol: object = None
    spot: float = 100.0
    curr_date: str = "2026-08-27"
    prev_date: str = "2026-08-26"


def _bearish_fa(d_spot=0.0, nc=10_000, npu=50_000):
    return _FA(changes=[_fc("P", "bearish", 1.0, -0.30) for _ in range(30)],
               upside_pressure=10_000.0, downside_pressure=90_000.0,
               net_call_doi=nc, net_put_doi=npu,
               vol=_Vol(d_spot_pct=d_spot, d_atm_pp=-1.0, d_skew25_pp=0.0))


def test_wing_weights_single_implementation():
    """probe 的主翼数必须 == wing_weights 的输出（两处不能各算各的）。"""
    fa = _bearish_fa()
    bull, bear = wing_weights(fa)
    p = probe_strong_signal(fa)
    assert p["bull_wing"] == round(bull, 2)
    assert p["bear_wing"] == round(bear, 2)
    print("PASS test_wing_weights_single_implementation")


def test_wing_excludes_out_of_band_delta():
    """主翼只认 0.18~0.45 Δ；深尾与 ATM 腿不得计入。"""
    fa = _FA(changes=[_fc("P", "bearish", 1.0, -0.05),    # 太 OTM
                      _fc("P", "bearish", 1.0, -0.70),    # 太 ITM
                      _fc("P", "bearish", 1.0, -0.30)])   # 在带内
    assert wing_weights(fa)[1] == 1.0
    print("PASS test_wing_excludes_out_of_band_delta")


def test_oi_build_ratio_exposes_two_sided_build():
    """建仓比是暴露「名为一边倒、实则两边都在建」的那个数。"""
    p = probe_strong_signal(_bearish_fa(nc=61_636, npu=78_473))   # 复刻 QQQ 2026-08-27
    assert p["oi_build_ratio"] == 1.27
    p2 = probe_strong_signal(_bearish_fa(nc=0, npu=5_000))        # 除零保护
    assert p2["oi_build_ratio"] == 5000.0
    print("PASS test_oi_build_ratio_exposes_two_sided_build")


def test_contra_margin_sign_convention():
    """余量 = 阈值 − 逆向幅度：正=还没触发（越小越险），负=已被拦下。"""
    p = probe_strong_signal(_bearish_fa(d_spot=+1.49))            # QQQ 当日
    assert abs(p["看跌"]["contra_margin"] - (STRONG_CONTRA_SPOT - 1.49)) < 1e-9
    assert p["看跌"]["contra_margin"] > 0                          # 擦边过关
    p2 = probe_strong_signal(_bearish_fa(d_spot=+2.50))
    assert p2["看跌"]["contra_margin"] < 0                         # 已拦下
    print("PASS test_contra_margin_sign_convention")


def test_probe_reports_no_vol_as_none_not_zero():
    """无波动率面时三个 Δ 必须是 None，不能用 0 冒充「没变化」。"""
    p = probe_strong_signal(_FA(vol=None))
    assert p["d_spot_pct"] is None and p["d_atm_pp"] is None
    assert p["看跌"]["contra_margin"] is None
    print("PASS test_probe_reports_no_vol_as_none_not_zero")


def test_record_is_daily_not_only_candidates(tmp_path):
    """必须逐日记录：只记"闸门全过"的那些，就永远无法校准 3×/4×/3,000 三个阈值。"""
    weak = _FA(changes=[_fc("P", "bearish", 1.0, -0.30)],
               upside_pressure=100.0, downside_pressure=110.0, net_call_doi=5, net_put_doi=7,
               vol=_Vol(d_spot_pct=0.1, d_atm_pp=0.1))
    row = sl.record("qqq", on_date="2026-08-27", prev_date="2026-08-26", spot=719.77,
                    probe=probe_strong_signal(weak), signal=None, root=tmp_path)
    assert row["fired"] is False
    assert len(sl.load_all(["qqq"], root=tmp_path)) == 1      # 没开火也要在库里
    # 连续比值必须落盘，否则画不出阈值-结果关系
    assert row["bear_pressure_ratio"] is not None and row["bear_wing_ratio"] is not None
    print("PASS test_record_is_daily_not_only_candidates")


def test_record_stores_continuous_ratios(tmp_path):
    row = sl.record("qqq", on_date="2026-08-27", prev_date="2026-08-26", spot=1.0,
                    probe=probe_strong_signal(_bearish_fa()), signal=None, root=tmp_path)
    assert row["bear_pressure_ratio"] == 9.0          # 90,000 / 10,000
    assert row["bear_gates"] == [True, True, True]
    print("PASS test_record_stores_continuous_ratios")


def test_unknown_outlook_never_becomes_diverges_true(tmp_path):
    """综合研判未知时 diverges 必须是 None —— 未知不许包装成事实。

    旧版本的实际后果：31 行全部 outlook_bias=""，却有 14 行 diverges=true，
    因为 _diverges 对空串执行 `key not in ""` 恒为真。
    """
    class _S:
        direction, level, vol_confirms, diverges = "看跌", "强", False, True
    row = sl.record("qqq", on_date="2026-08-27", prev_date="2026-08-26", spot=1.0,
                    probe=probe_strong_signal(_bearish_fa()), signal=_S(),
                    outlook_bias="", root=tmp_path)
    assert row["diverges"] is None and row["outlook_bias"] is None
    row2 = sl.record("qqq", on_date="2026-08-28", prev_date="2026-08-27", spot=1.0,
                     probe=probe_strong_signal(_bearish_fa()), signal=_S(),
                     outlook_bias="偏多", root=tmp_path)
    assert row2["diverges"] is True
    print("PASS test_unknown_outlook_never_becomes_diverges_true")


def test_record_upserts_same_date(tmp_path):
    p = probe_strong_signal(_bearish_fa())
    for spot in (700.0, 719.77):
        sl.record("qqq", on_date="2026-08-27", prev_date="2026-08-26", spot=spot,
                  probe=p, signal=None, root=tmp_path)
    rows = sl.load_all(["qqq"], root=tmp_path)
    assert len(rows) == 1 and rows[0]["spot"] == 719.77
    print("PASS test_record_upserts_same_date")


def test_clear_enables_true_rebuild(tmp_path):
    """--rebuild 必须真清空：旧定义下的行残留会让统计变成版本混合，不可解释。"""
    p = probe_strong_signal(_bearish_fa())
    sl.record("qqq", on_date="2026-08-27", prev_date="2026-08-26", spot=1.0,
              probe=p, signal=None, root=tmp_path)
    assert sl.clear("qqq", root=tmp_path) == 1
    assert sl.load_all(["qqq"], root=tmp_path) == []
    print("PASS test_clear_enables_true_rebuild")


def test_record_stores_gap_days(tmp_path):
    """所有 Δ 都是相邻【快照】之差；跨假期的间隔必须如实记下供日后分层。"""
    p = probe_strong_signal(_bearish_fa())
    row = sl.record("qqq", on_date="2026-08-18", prev_date="2026-08-15", spot=1.0,
                    probe=p, signal=None, root=tmp_path)
    assert row["gap_days"] == 3          # 日历日；交易日数由 backfill 填 trading_gap
    assert row["trading_gap"] is None
    print("PASS test_record_stores_gap_days")


def test_backfill_never_fabricates_future(tmp_path):
    """前瞻窗口没走完 → 留 null，绝不用最后一根价冒充未来。"""
    p = probe_strong_signal(_bearish_fa())
    sl.record("qqq", on_date="2026-08-27", prev_date="2026-08-26", spot=100.0,
              probe=p, signal=None, root=tmp_path)
    sl.backfill("qqq", [date(2026, 8, 27)], [100.0], root=tmp_path)
    r = sl.load_all(["qqq"], root=tmp_path)[0]
    assert all(r[f"forward_{h}d"] is None for h in sl.HORIZONS)
    assert r["base_date"] is None          # 序列没走过 D，基准都不该定
    print("PASS test_backfill_never_fabricates_future")


def test_backfill_computes_regime(tmp_path):
    p = probe_strong_signal(_bearish_fa())
    n = sl.MA_N + sl.DRIFT_N + 20
    dates = [date.fromordinal(date(2025, 1, 1).toordinal() + i) for i in range(n)]
    closes = [100.0 + i for i in range(n)]                  # 单调上行 → 牛
    tgt = n - 15
    sl.record("qqq", on_date=dates[tgt].isoformat(), prev_date=dates[tgt-1].isoformat(),
              spot=closes[tgt], probe=p, signal=None, root=tmp_path)
    sl.backfill("qqq", dates, closes, root=tmp_path)
    r = sl.load_all(["qqq"], root=tmp_path)[0]
    assert r["regime"] == "牛"
    assert r["drift_60d"] is not None
    assert r["forward_10d"] is not None                     # 10 日窗口也走完了
    print("PASS test_backfill_computes_regime")


def test_summarize_requires_min_n_and_p(tmp_path):
    """样本不够就必须 conclusive=False —— 即便准确率 100%。"""
    rows = [{"instrument": "qqq", "date": f"2026-0{1+i//4}-{10+i:02d}",
             "direction": "看跌", "fired": True, "level": "强",
             "forward_5d": -5.0, "drift_60d": 0.0, "oi_build_ratio": 2.0}
            for i in range(10)]
    st = sl.summarize(rows, horizon=5)
    assert st["全部开火"]["acc_pct"] == 100.0
    assert st["全部开火"]["conclusive"] is False            # n=10 < MIN_N
    print("PASS test_summarize_requires_min_n_and_p")


def test_summarize_detrend_drops_rows_without_drift(tmp_path):
    """去趋势模式下漂移缺失的行必须剔除，不能用 0 顶替（那等于偷偷假设无趋势）。"""
    rows = [{"instrument": "qqq", "date": "2026-08-27", "direction": "看跌",
             "fired": True, "level": "强",
             "forward_5d": -5.0, "drift_60d": None, "oi_build_ratio": 2.0}]
    assert sl.summarize(rows, horizon=5) == {}
    assert sl.summarize(rows, horizon=5, detrend=False)["全部开火"]["n"] == 1
    print("PASS test_summarize_detrend_drops_rows_without_drift")


def test_binom_two_tail_sanity():
    assert abs(sl._binom_two_tail(5, 10) - 1.0) < 1e-9
    assert sl._binom_two_tail(0, 10) < 0.01
    assert sl._binom_two_tail(10, 10) == sl._binom_two_tail(0, 10)   # 对称
    print("PASS test_binom_two_tail_sanity")


def test_corrupted_ledger_is_quarantined_not_overwritten(tmp_path):
    """损坏的台账必须被隔离保留，绝不能被下一次写入静默抹掉 —— 历史不可再生。"""
    d = tmp_path / "history" / "signals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "qqq.json").write_text("{not json", encoding="utf-8")
    sl.record("qqq", on_date="2026-08-27", prev_date="2026-08-26", spot=1.0,
              probe=probe_strong_signal(_bearish_fa()), signal=None, root=tmp_path)
    assert (d / "qqq.json.corrupt-1").exists()                 # 旧数据还在
    assert (d / "qqq.json.corrupt-1").read_text() == "{not json"
    assert len(sl.load_all(["qqq"], root=tmp_path)) == 1       # 新表正常
    print("PASS test_corrupted_ledger_is_quarantined_not_overwritten")


def test_backfill_base_is_pre_signal_close(tmp_path):
    """基准必须是快照日【之前】最后一个已知收盘 —— 用 close[D] 定基就是前视。

    快照文件名日期 D 实际是 ET 01:00~02:00 抓的，装的是 D-1 结算的 OI，
    报告在 D 开盘前可读 → 信号在 D 开盘才可执行。
    """
    n = sl.MA_N + sl.DRIFT_N + 20
    dates = [date.fromordinal(date(2025, 1, 1).toordinal() + i) for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    tgt = n - 15
    sl.record("qqq", on_date=dates[tgt].isoformat(), prev_date=dates[tgt - 1].isoformat(),
              spot=closes[tgt], probe=probe_strong_signal(_bearish_fa()),
              signal=None, root=tmp_path)
    sl.backfill("qqq", dates, closes, root=tmp_path)
    r = sl.load_all(["qqq"], root=tmp_path)[0]
    assert r["base_date"] == dates[tgt - 1].isoformat()        # D-1，不是 D
    assert r["base_close"] == closes[tgt - 1]
    exp = (closes[tgt - 1 + 5] / closes[tgt - 1] - 1) * 100    # 从 D-1 起算
    assert abs(r["forward_5d"] - exp) < 1e-4
    print("PASS test_backfill_base_is_pre_signal_close")


def test_summarize_rejects_unsupported_horizon():
    try:
        sl.summarize([], horizon=2)
    except ValueError:
        print("PASS test_summarize_rejects_unsupported_horizon"); return
    raise AssertionError("horizon=2 应当报错而不是静默返回空统计")


def test_thin_is_per_instrument(tmp_path):
    """抽稀必须按品种独立 —— 跨品种用全局序号抽会把不同标的混在一条时间轴上。"""
    mk = lambda k, d: ({"instrument": k, "date": d, "direction": "看跌"}, -1.0)
    items = [mk("gold", "2026-08-03"), mk("qqq", "2026-08-03"),
             mk("gold", "2026-08-04"), mk("qqq", "2026-08-04")]
    kept = sl._thin(items, 5)
    assert len(kept) == 2                                     # 每个品种各留 1 条
    assert {r["instrument"] for r, _ in kept} == {"gold", "qqq"}
    print("PASS test_thin_is_per_instrument")


if __name__ == "__main__":
    import shutil, tempfile
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        tmp = Path(tempfile.mkdtemp())
        try:
            fn(tmp) if fn.__code__.co_argcount else fn()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fns)} passed")
