"""墙 × 资金流 —— 回归测试。

锚点是 2026-09-17 的那次真实误报：按 OI 排名取 top-3 展示墙位，导致
「56 仍在但掉出前三」被读成「墙没动」，同时漏掉了 53/52.5/50 一天新增 28,800 张
的防线迁移。所以这里锁死两件事：**按距离取、带 ΔOI**。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.gamma import wall_flow, render_wall_flow, WALL_FLOW_MIN_OI  # noqa: E402
from undertow.core.models import OptionsSnapshot, OptionContract                   # noqa: E402

TODAY = date(2026, 9, 16)
EXP = TODAY + timedelta(days=7)


def _snap(spot, rows):
    """rows: [(strike, kind, oi)]"""
    cs = [OptionContract(expiry=EXP, strike=s, kind=k, open_interest=oi,
                         volume=10, gamma=0.01, delta=0.3, iv=0.40)
          for s, k, oi in rows]
    return OptionsSnapshot(instrument="t", proxy_symbol="T", spot=spot,
                           asof="2026-09-16", contracts=cs)


def test_picks_by_distance_not_by_oi_rank():
    """必须按距现价远近取，不按 OI 排名 —— 这是那次误报的直接原因。

    远虚的巨型堆积（长期尾部保险）永远排在 OI 前列，但对交易毫无意义；
    真正该看见的是"最近的那道在哪"。
    """
    curr = _snap(57.0, [
        (56.0, "P", 14_000), (55.0, "P", 34_000), (50.0, "P", 200_000),
        (58.0, "C", 7_000), (60.0, "C", 230_000),
    ])
    rows = wall_flow(None, curr, TODAY, 57.0, n_side=2)
    # ⚠️ 选取按【距离】，但输出按行权价降序（渲染要从高到低画），两者不是一回事
    sup = {r.strike for r in rows if r.side == "支撑"}
    res = {r.strike for r in rows if r.side == "阻力"}
    assert sup == {56.0, 55.0}, f"应取最近两道 56/55，而非 OI 最大的 50，得到 {sup}"
    assert res == {58.0, 60.0}, f"应取最近两道，得到 {res}"
    assert 50.0 not in sup, "OI 20 万但距离 -12%，不该挤掉贴身的墙"
    # 输出顺序：行权价从高到低
    assert [r.strike for r in rows] == sorted((r.strike for r in rows), reverse=True)


def test_reports_delta_oi_so_migration_is_visible():
    """防线迁移必须看得见：56 撤、53 建，这正是 top-N 排名会漏掉的信息。"""
    prev = _snap(58.0, [(56.0, "P", 19_507), (53.0, "P", 5_461), (55.0, "P", 32_484)])
    curr = _snap(57.0, [(56.0, "P", 14_315), (53.0, "P", 17_607), (55.0, "P", 33_939)])
    rows = {r.strike: r for r in wall_flow(prev, curr, TODAY, 57.0, n_side=4)}
    assert rows[56.0].d_oi == -5_192, "56 撤了 5,192，必须报出来"
    assert rows[53.0].d_oi == +12_146, "53 建了 12,146，必须报出来"
    assert rows[55.0].d_oi == +1_455
    txt = render_wall_flow(list(rows.values()), 57.0)
    assert "↓撤" in txt and "↑建" in txt, "方向箭头要能一眼区分建/撤"


def test_min_oi_filters_noise_not_real_walls():
    """OI 下限只滤零碎，不能把真墙滤掉。"""
    curr = _snap(57.0, [(56.5, "P", 50), (56.0, "P", 14_000)])
    rows = wall_flow(None, curr, TODAY, 57.0)
    strikes = [r.strike for r in rows]
    assert 56.5 not in strikes, f"OI 50 是零碎，不该算一道墙（门槛 {WALL_FLOW_MIN_OI}）"
    assert 56.0 in strikes


def test_no_prev_snapshot_degrades_gracefully():
    """没有对照快照时 d_oi 归零、不得崩 —— 首日快照就是这种情况。"""
    curr = _snap(57.0, [(56.0, "P", 14_000), (58.0, "C", 7_000)])
    rows = wall_flow(None, curr, TODAY, 57.0)
    assert rows and all(r.d_oi == 0 for r in rows)
    assert all(r.judgments == [] for r in rows)
    render_wall_flow(rows, 57.0)     # 不抛异常即可


def test_spot_line_sits_between_resistance_and_support():
    """渲染时现价那行必须夹在阻力与支撑之间，否则读图会反。"""
    curr = _snap(57.0, [(56.0, "P", 14_000), (58.0, "C", 7_000)])
    txt = render_wall_flow(wall_flow(None, curr, TODAY, 57.0), 57.0)
    lines = txt.split("\n")
    i_res = next(i for i, x in enumerate(lines) if "阻力" in x)
    i_spot = next(i for i, x in enumerate(lines) if "─────" in x)   # 表头也含「距现价」
    i_sup = next(i for i, x in enumerate(lines) if "支撑" in x)
    assert i_res < i_spot < i_sup


def test_judgment_is_not_truncated_mid_word():
    """细判不得字符级截断 —— 截一半会出现「买方(」这种残句。"""
    from undertow.analyze.gamma import WallFlowRow
    r = WallFlowRow(strike=56.0, side="支撑", oi=14_000, d_oi=-5_192, dist_pct=-1.8,
                    judgments=["卖方做支撑", "买方了结", "噪音", "买方保护(新建·主动方未知)"])
    txt = render_wall_flow([r], 57.0)
    assert "噪音" not in txt, "噪音不是资金动向，不该显示"
    for frag in ("买方(", "存疑(该"):
        assert not txt.rstrip().endswith(frag), "不得截成残句"
