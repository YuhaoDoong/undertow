"""重复实现的漂移守卫。

仓库里有若干同名私有工具函数分散在多个模块（历史原因）。它们【现在】数值一致，
但一致性没有任何机制保障——只要有人改其中一处，两个模块就会静默算出不同的数。

今晚（2026-08-26）已经踩到两次同类问题：
  · verdict.py 把「详细结论」和「总纲标签」写成两套并行 if 链，漂出三处自相矛盾
  · stretch_backtest.py 复制了一份 _atr_series（已合并回 stretch.py）
外部佐证：BetaGold 仓库里 _stoch_rsi 存在三份独立实现，阈值已各自漂移。

本测试不重构，只钉住不变式：**同一个量，不同模块必须算出同一个数**。
哪天有人只改一处，这里立刻挂。
"""
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze import flow, gamma, stretch, technicals


def _series(n=140, seed=11):
    random.seed(seed)
    c = [100.0]
    for _ in range(n):
        c.append(c[-1] * (1 + random.gauss(0, 0.02)))
    return [x * 1.012 for x in c], [x * 0.988 for x in c], c


def test_yearfrac_flow_vs_gamma():
    """到期年化时间：flow 与 gamma 必须一致，否则两层会算出不同 DTE 权重。"""
    today = dt.date(2026, 8, 26)
    for d in range(0, 120, 3):
        e = today + dt.timedelta(days=d)
        assert abs(flow._yearfrac(e, today) - gamma._yearfrac(e, today)) < 1e-12, d
    print("PASS test_yearfrac_flow_vs_gamma")


def test_atr_stretch_vs_technicals():
    """ATR：stretch 与 technicals 必须一致——它同时是超买超卖的分母和技术面的读数。"""
    h, l, c = _series()
    for i in range(30, len(c), 7):
        a = stretch._atr(h[:i + 1], l[:i + 1], c[:i + 1], 14)
        b = technicals._atr(h[:i + 1], l[:i + 1], c[:i + 1], 14)
        assert (a is None) == (b is None), i
        if a is not None:
            assert abs(a - b) < 1e-12, (i, a, b)
    print("PASS test_atr_stretch_vs_technicals")


def test_sma_stretch_vs_technicals():
    _h, _l, c = _series()
    for n in (5, 10, 20, 30, 200):
        for i in range(35, len(c), 11):
            a, b = stretch._sma(c[:i + 1], n), technicals._sma(c[:i + 1], n)
            assert (a is None) == (b is None), (n, i)
            if a is not None:
                assert abs(a - b) < 1e-12, (n, i, a, b)
    print("PASS test_sma_stretch_vs_technicals")


def test_atr_series_is_single_implementation():
    """滚动 ATR 只能有一份实现——stretch_backtest 必须直接引用 stretch 的那个对象。"""
    from undertow.analyze import stretch_backtest as sb
    assert sb._atr_series is stretch._atr_series, "又出现了第二份 _atr_series 实现"
    print("PASS test_atr_series_is_single_implementation")


def test_rolling_atr_matches_pointwise_reference():
    """滚动版必须等于逐点参考实现——这是回测与线上读数一致的前提。"""
    h, l, c = _series(220, seed=7)
    fast = stretch._atr_series(h, l, c, 14)
    for i in range(20, len(c), 9):
        ref = stretch._atr(h[:i + 1], l[:i + 1], c[:i + 1], 14)
        assert fast[i] is not None and abs(fast[i] - ref) < 1e-9, (i, fast[i], ref)
    print("PASS test_rolling_atr_matches_pointwise_reference")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
