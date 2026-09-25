"""长桥期权链备份源 —— 回归测试。

两个 bug 都是 2026-09-24 首测时踩出来的，且都会【静默】给出错误的 delta：

① 用 UTC date 当交易日 —— ET 22:18 时 UTC 已跨日，当天到期的合约被算成 T=0，
   delta 全部归零（深度实值 put 本该 −1，实测误差 1.0000）。
   每天有约 8 小时窗口会踩到。
② 没剔除已过期的到期日 —— 长桥 chain 仍返回它们，T<0 同样让 delta 归零，
   还把大量请求浪费在对分析无意义的合约上。

修复后主翼区间（|Δ|∈[0.18,0.45]，方向判定实际用的那段）最大误差
从 0.3298 降到 0.0885。
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.collect import longbridge_options as lo   # noqa: E402


def test_symbol_formats_differ_only_by_padding():
    """OCC 补零到 8 位、长桥不补零 —— 这个差异曾让首次比对全部落空。"""
    exp = date(2026, 9, 25)
    assert lo._occ("SLV", exp, "P", 57.0) == "SLV260925P00057000"
    assert lo._lb_symbol("SLV", exp, "P", 57.0) == "SLV260925P57000.US"
    assert lo._occ("GLD", exp, "C", 405.0) == "GLD260925C00405000"
    assert lo._lb_symbol("GLD", exp, "C", 405.0) == "GLD260925C405000.US"
    # 半档行权价不能丢精度
    assert lo._occ("SLV", exp, "C", 57.5) == "SLV260925C00057500"


def test_deep_itm_put_delta_is_minus_one():
    """深度实值 put 的 delta 必须接近 −1，不能是 0。

    这正是 UTC/ET 混用时的症状：T=0 → delta 归零 → 净敞口折算整体偏掉。
    """
    d, g = lo._greeks(S=57.72, K=69.0, T=1 / 365, sigma=0.574, kind="P")
    assert -1.0001 < d < -0.99, f"深度实值 put delta 应 ≈ −1，得到 {d}"
    d2, _ = lo._greeks(S=57.72, K=40.0, T=30 / 365, sigma=0.35, kind="C")
    assert d2 > 0.99, f"深度实值 call delta 应 ≈ +1，得到 {d2}"


def test_atm_delta_near_half_and_gamma_positive():
    d, g = lo._greeks(S=100.0, K=100.0, T=30 / 365, sigma=0.30, kind="C")
    assert 0.5 < d < 0.58, d
    assert g > 0, "gamma 恒为正"
    dp, gp = lo._greeks(S=100.0, K=100.0, T=30 / 365, sigma=0.30, kind="P")
    assert abs((d - dp) - 1.0) < 1e-9, "put-call delta 差必须恒为 1"
    assert abs(g - gp) < 1e-12, "call/put 的 gamma 相同"


def test_greeks_degrade_to_zero_on_invalid_input():
    """无效输入返回 (0,0) 而不是抛异常或 NaN —— 缺数据不能污染下游。"""
    for args in ((0.0, 100.0, 0.1, 0.3), (100.0, 0.0, 0.1, 0.3),
                 (100.0, 100.0, 0.0, 0.3), (100.0, 100.0, 0.1, 0.0)):
        assert lo._greeks(*args, kind="C") == (0.0, 0.0)


def test_uses_market_today_not_utc():
    """必须用美东交易日，不能用 UTC date。"""
    src = (ROOT / "undertow" / "collect" / "longbridge_options.py").read_text("utf-8")
    assert "market_today()" in src, "须用 core.clock.market_today"
    assert "datetime.now(timezone.utc).date()" not in src, \
        "不得用 UTC date 当交易日——ET 20:00 后 UTC 已跨日"


def test_expired_series_filtered():
    """已过期的到期日必须剔除 —— T<0 会让 delta 归零。"""
    src = (ROOT / "undertow" / "collect" / "longbridge_options.py").read_text("utf-8")
    assert "e >= today" in src, "须过滤 expiry < today"


def test_batch_size_and_fallback_documented():
    """批量 50 + 二分降级是实测约束（100 在 GLD 直接 HTTP 500）。"""
    assert lo.QUOTE_BATCH <= 50, "实测 100 在 GLD 会 500，必须 ≤50"
    src = (ROOT / "undertow" / "collect" / "longbridge_options.py").read_text("utf-8")
    assert "bad.append" in src, "单个仍失败的代码必须回传，不得静默丢弃"


def test_payload_marks_computed_greeks():
    """payload 必须自报 delta/gamma 是算出来的，不是源给的。"""
    src = (ROOT / "undertow" / "collect" / "longbridge_options.py").read_text("utf-8")
    assert '"_greeks": "bs_computed"' in src
    assert '"_missing_quote"' in src, "取不到报价的代码须可审计"
    # 盘口留 0 而非编造
    assert '"bid": 0.0, "ask": 0.0' in src


def test_collect_layer_does_not_import_analyze():
    """collect 不得依赖 analyze —— 单向依赖是项目的代码地图约束。"""
    src = (ROOT / "undertow" / "collect" / "longbridge_options.py").read_text("utf-8")
    assert "from undertow.analyze" not in src and "import undertow.analyze" not in src
