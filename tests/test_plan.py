"""计划交易的确定性测试（函数式，不依赖 pytest / 网络）。

锚定：触发判定、接近告警、出场三要素完整性、下单参数渲染（且不含任何执行动作）。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.soul.plan import (TradePlan, Leg, Exits, check_plans, render_orders,
                                render_plans_md, load_plans, save_plans)


def _plan(level=60.0, direction="below", exits=None):
    return TradePlan(
        id="t1", underlying="SLV", structure="牛市看跌价差 卖58P/买56P",
        level=level, direction=direction,
        legs=[Leg("SLV260918P58000.US", "sell", 1, 1.28),
              Leg("SLV260918P56000.US", "buy", 1, 0.70)],
        exits=exits or Exits(target="收回50%权金", stop="2倍权金", time="剩21天", edge="边际≤0"),
        gate="卖方边际 ≥10pp", size_note="1张", status="waiting", created="2026-08-25")


def test_trigger_below():
    p = _plan(60.0, "below")
    assert p.triggered(59.5) is True
    assert p.triggered(61.0) is False
    assert p.triggered(None) is None
    alerts = check_plans([p], {"SLV": 59.5})
    assert alerts and alerts[0].kind == "触发", alerts
    print(f"PASS test_trigger_below → {alerts[0].detail}")


def test_near_alert():
    """距触发价 1% 内 → 接近告警（不是触发）。"""
    alerts = check_plans([_plan(60.0, "below")], {"SLV": 60.5})
    assert alerts and alerts[0].kind == "接近", alerts
    print(f"PASS test_near_alert → {alerts[0].detail}")


def test_incomplete_exits_warns():
    """出场三要素缺失 → 渲染里明确警告『不应进场』。"""
    p = _plan(exits=Exits(target="收50%"))       # 缺 stop / time
    assert not p.exits.complete
    md = render_orders(p)
    assert "不应进场" in md, md
    print("PASS test_incomplete_exits_warns")


def test_orders_are_params_only():
    """下单参数只是文本参考，且明确声明工具不执行交易。"""
    md = render_orders(_plan())
    assert "SLV260918P58000.US" in md and "LO（限价）" in md
    assert "下单由你自己在长桥端完成" in md and "不执行交易" in md, md
    print("PASS test_orders_are_params_only")


def test_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "plans.json"
        save_plans([_plan()], fp)
        got = load_plans(fp)
    assert len(got) == 1 and got[0].id == "t1" and len(got[0].legs) == 2
    assert got[0].exits.complete
    print("PASS test_roundtrip")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
