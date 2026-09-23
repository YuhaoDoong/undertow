"""台账 MFE/MAE —— 回归测试。

锚点是 2026-09-23 用户的提问：「盘中跌下去又被拉回来的，统计里算失败吗？」
答案是【是】，而这正是 forward_Nd 的设计（收盘、可执行、无事后信息）。
补 MFE/MAE 是为了把「走出来过没有」和「中途被打多深」记下来，
**但绝不能让它们污染命中率口径** —— MFE 是事后最优，实盘拿不到。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze import signal_ledger as sl   # noqa: E402

D0 = date(2026, 9, 1)


def _series(n=12):
    """构造 n 天价格：close 全 100，便于手算。"""
    ds = [D0 + timedelta(days=i) for i in range(n)]
    return ds, [100.0] * n


def _row(tmp, direction, day_idx=3):
    ds, cs = _series()
    sl.clear(  "t", root=tmp) if hasattr(sl, "clear") else None
    r = sl.record("t", on_date=ds[day_idx].isoformat(),
                  prev_date=ds[day_idx - 1].isoformat(), spot=100.0,
                  probe=None, signal=None, root=tmp)
    return ds, cs, r


def test_mfe_mae_signed_by_direction(tmp_path):
    """MFE/MAE 必须【按信号方向取号】：正 = 朝信号方向走。

    看跌信号在价格【下跌】时 MFE 为正 —— 若不取号，看跌信号的有利波动
    会被记成负数，与看涨信号无法放在同一列比较。
    """
    ds, cs = _series()
    highs = [100.0] * len(cs)
    lows = [100.0] * len(cs)
    # 信号日 D=ds[3] → 基准 i=2（D 之前最后一个收盘），窗口 [i+1, i+h] = [3,3]
    # 所以极值要设在 index 3，不是 4
    highs[3], lows[3] = 102.0, 97.0

    for direction, want_mfe, want_mae in (("看跌", +3.0, -2.0), ("看涨", +2.0, -3.0)):
        root = tmp_path / direction
        rows = [{"date": ds[3].isoformat(), "prev_date": ds[2].isoformat(),
                 "direction": direction, "fired": True,
                 **{f"forward_{h}d": None for h in sl.HORIZONS},
                 **{f"mfe_{h}d": None for h in sl.HORIZONS},
                 **{f"mae_{h}d": None for h in sl.HORIZONS}}]
        root.mkdir(parents=True, exist_ok=True)
        sl._save("t", rows, root)
        sl.backfill("t", ds, cs, root=root, highs=highs, lows=lows)
        got = sl._load("t", root)[0]
        assert abs(got["mfe_1d"] - want_mfe) < 1e-6, f"{direction} MFE {got['mfe_1d']}"
        assert abs(got["mae_1d"] - want_mae) < 1e-6, f"{direction} MAE {got['mae_1d']}"
    print("PASS test_mfe_mae_signed_by_direction")


def test_window_excludes_base_day(tmp_path):
    """窗口是 [i+1, i+h]，**不含基准日自身**。

    基准是 D 之前的收盘、信号在 D 开盘才可执行；把 D−1 的盘中极值算进去就是前视。
    """
    ds, cs = _series()
    highs = [100.0] * len(cs); lows = [100.0] * len(cs)
    highs[3], lows[3] = 150.0, 50.0      # 基准日(i=3)的极值必须被忽略
    rows = [{"date": ds[4].isoformat(), "prev_date": ds[3].isoformat(),
             "direction": "看涨", "fired": True,
             **{f"forward_{h}d": None for h in sl.HORIZONS},
             **{f"mfe_{h}d": None for h in sl.HORIZONS},
             **{f"mae_{h}d": None for h in sl.HORIZONS}}]
    sl._save("t", rows, tmp_path)
    sl.backfill("t", ds, cs, root=tmp_path, highs=highs, lows=lows)
    got = sl._load("t", tmp_path)[0]
    assert abs(got["mfe_1d"]) < 1e-6, f"基准日极值泄漏进 MFE：{got['mfe_1d']}"
    assert abs(got["mae_1d"]) < 1e-6
    print("PASS test_window_excludes_base_day")


def test_backfill_without_ohlc_still_works(tmp_path):
    """highs/lows 缺省 → 跳过 MFE/MAE，其余回填不受影响（向后兼容）。"""
    ds, cs = _series()
    rows = [{"date": ds[3].isoformat(), "prev_date": ds[2].isoformat(),
             "direction": "看涨", "fired": True,
             **{f"forward_{h}d": None for h in sl.HORIZONS},
             **{f"mfe_{h}d": None for h in sl.HORIZONS},
             **{f"mae_{h}d": None for h in sl.HORIZONS}}]
    sl._save("t", rows, tmp_path)
    filled, _ = sl.backfill("t", ds, cs, root=tmp_path)
    got = sl._load("t", tmp_path)[0]
    assert filled >= 1 and got["forward_1d"] is not None
    assert got["mfe_1d"] is None and got["mae_1d"] is None
    print("PASS test_backfill_without_ohlc_still_works")


def test_no_direction_means_no_mfe(tmp_path):
    """未开火/无方向的行不得有 MFE/MAE —— 没有方向就无从取号，折 0 会是假数据。"""
    ds, cs = _series()
    highs = [100.0] * len(cs); lows = [100.0] * len(cs)
    highs[3], lows[3] = 102.0, 97.0
    rows = [{"date": ds[3].isoformat(), "prev_date": ds[2].isoformat(),
             "direction": None, "fired": False,
             **{f"forward_{h}d": None for h in sl.HORIZONS},
             **{f"mfe_{h}d": None for h in sl.HORIZONS},
             **{f"mae_{h}d": None for h in sl.HORIZONS}}]
    sl._save("t", rows, tmp_path)
    sl.backfill("t", ds, cs, root=tmp_path, highs=highs, lows=lows)
    got = sl._load("t", tmp_path)[0]
    assert got["mfe_1d"] is None and got["mae_1d"] is None
    print("PASS test_no_direction_means_no_mfe")


def test_cboe_source_populates_ohlc():
    """CBOE 历史源必须填 highs/lows —— 否则回填拿不到，MFE/MAE 永远是 None。"""
    import inspect
    from undertow.collect import cboe_history
    src = inspect.getsource(cboe_history)
    assert "highs=" in src and "lows=" in src, "PriceSeries 必须带 highs/lows"
    assert "lo <= c <= h" in src, "极值与收盘矛盾的脏行必须丢弃，不可污染 MAE"
    print("PASS test_cboe_source_populates_ohlc")


def test_docstring_warns_mfe_is_hindsight():
    """文档必须写明 MFE 是事后最优、不得当战绩 —— 这是最容易被误用的字段。"""
    doc = sl.backfill.__doc__
    assert "事后最优" in doc and "实盘拿不到" in doc
    assert "forward_Nd" in doc, "必须点明命中率一律以收盘口径为准"
    print("PASS test_docstring_warns_mfe_is_hindsight")
