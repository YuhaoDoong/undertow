"""新研报体系 v2：准入规则（观测类可上、方向类必须 T1）与渲染。"""
from datetime import date

from undertow.analyze.expiry_ladder import wall_overview
from undertow.core.models import OptionContract, OptionsSnapshot
from undertow.report import v2


def test_prediction_sections_need_t1():
    ok, _ = v2.section_allowed(v2.Section("x", "某信号", "prediction", claim_id="不存在的主张"))
    assert not ok
    ok, why = v2.section_allowed(v2.Section("walls", "墙", "observation"))
    assert ok and "观测" in why
    assert all(s.role == "observation" for s in v2.SECTIONS)            # 目前没有方向信号达到 T1


def test_render_only_walls_and_missing_items():
    snap = OptionsSnapshot(instrument="gold", proxy_symbol="GLD", spot=380.0, asof="t",
                           contracts=[OptionContract(expiry=date(2026, 9, 30), strike=375, kind="P", open_interest=7000,
                                                     volume=0, gamma=0.01, delta=-0.3, iv=0.2)])
    items = [{"name": "黄金", "symbol": "GLD", "status": "ok", "overview": wall_overview(snap, today=date(2026, 9, 29)),
              "ratio": 10.9, "conv": lambda x: x * 10.9, "captured_at": "2026-09-29 06:00 ET"},
             {"name": "白银", "symbol": "SLV", "status": "missing", "why": "快照未到"}]
    h = v2.render_html("2026-09-29", items, generated_at="now")
    assert "期权墙总览" in h and "目前没有任何方向信号达到 T1" in h and "快照未到" in h and "≈4088" in h
    md = v2.render_md("2026-09-29", items, generated_at="now")
    assert "| 2026-09-30 | 季度 | 1 |" in md and "⚠️ 快照未到" in md and "10.9000" in md


def test_daily_generates_v2_after_old_report_without_blocking():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "daily_update.sh").read_text("utf-8")
    assert src.index("python3 -m undertow report gold") < src.index("python3 -m undertow report-v2")
    assert "V2_RC != 0 && V2_RC != 3" in src and src.index("alert() {") < src.index("report-v2")
