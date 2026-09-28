"""structure_replay：防守轴粗分、方向映射、命中计分（基准=同期上涨率）、二项检验。"""
from scripts import structure_replay as R


def test_coarse_and_lean():
    assert [R.coarse(x) for x in ("进攻", "中性", "中性偏防守", "短线偏防守", "恐慌防守", None)] == \
        ["进攻", "中性", "防守", "防守", "防守", None]
    assert [R.lean(x) for x in ("进攻", "中性", "防守", None)] == [1, 0, -1, 0]
    assert [R.call_lean(x) for x in ("偏多", "偏空", "防守", "中性", "区间")] == [1, -1, -1, 0, 0]


def _row(state, r1):
    return {"state": state, "outcome": {"ret_1d": r1}}


def test_score_hits_and_base_rate():
    rows = [_row("进攻", 0.01), _row("进攻", -0.01), _row("防守", -0.02), _row("中性", 0.03), _row("防守", None)]
    s = R.score(rows, lambda r: R.lean(r["state"]), horizons=(1,))["1d"]
    assert (s["n"], s["hits"], s["n_up"], s["n_down"]) == (3, 2, 2, 1)       # 中性不计、未成熟不计
    # 同期上涨率 = 2/4（含中性那天）；基准 = (0.5+0.5+0.5)/3
    assert s["period_up_rate"] == 0.5 and s["base_rate"] == 0.5
    assert abs(s["mean_signed_ret_pct"] - (0.01 - 0.01 + 0.02) / 3 * 100) < 1e-3      # 输出保留 3 位


def test_binom_two_sided_known_values():
    assert R.binom_two_sided(5, 10, 0.5) == 1.0
    assert abs(R.binom_two_sided(9, 10, 0.5) - 0.021484375) < 1e-12
    assert R.binom_two_sided(0, 0, 0.5) is None
