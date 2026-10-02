"""研报 v2：准入规则（观测可上；方向判断只有 T1 或「验证中」两种身份）、首页与详情页渲染、daily 接线。"""
from datetime import date
from pathlib import Path

from undertow.analyze.expiry_ladder import wall_overview
from undertow.core.models import OptionContract, OptionsSnapshot
from undertow.report import v2

ROOT = Path(__file__).resolve().parents[1]


def test_gate_roles():
    ok, _ = v2.section_allowed(v2.Section("x", "某信号", "prediction", ("不存在的主张",)))
    assert not ok                                                              # 方向类未达 T1 → 不上
    ok, why = v2.section_allowed(v2.Section("d", "方向", "validating", ("conviction.h1.v1", "skew_reading.v1")))
    assert ok and "验证中" in why                                              # 前瞻预登记研究 → 可作「验证中」展示
    ok, _ = v2.section_allowed(v2.Section("d", "方向", "validating", ("outlook.mid_bias",)))
    assert not ok                                                              # 未登记为前瞻预登记的 T3 读数不能冒充「验证中」
    assert all(v2.section_allowed(s)[0] for s in v2.SECTIONS)


def _item(status="ok"):
    snap = OptionsSnapshot(instrument="gold", proxy_symbol="GLD", spot=380.0, asof="t",
                           contracts=[OptionContract(expiry=date(2026, 9, 30), strike=375, kind="P", open_interest=7000,
                                                     volume=0, gamma=0.01, delta=-0.3, iv=0.2)])
    if status != "ok":
        return {"name": "白银", "symbol": "SLV", "status": "missing", "why": "快照未到"}
    return {"key": "gold", "name": "黄金", "symbol": "GLD", "status": "ok", "detail_href": "v2_d_gold.html",
            "overview": wall_overview(snap, today=date(2026, 9, 29)), "ratio": 10.9, "conv": lambda x: x * 10.9,
            "captured_at": "2026-09-29 06:00 ET", "levels": [], "price_svg": "<svg></svg>", "oi_svg": "<svg></svg>",
            "layers_html": '<div class="card"><h2>① 期权结构 · 按到期分层（近端置顶）</h2></div>',
            "history_html": '<div class="card"><h2>③ 墙位历史 · 这墙守住过没有</h2></div>',
            "direction": {"conviction": {"reading": "无", "features": {"S": -1, "F": 1, "V": -1, "H1": 0},
                                         "quote_day": "2026-09-28", "recorded_at": "2026-09-29T10:03"},
                          "skew": {"reading": "防守化", "features": {"expiry": "2026-11-06"}},
                          "progress": {"start": "2026-09-29", "sessions": 1, "events": 0}}}


def test_index_links_and_validating_label():
    h = v2.render_index_html("2026-09-29", [_item(), _item("missing")], generated_at="now")
    assert 'href="v2_d_gold.html"' in h and "验证中" in h and "偏斜：防守化" in h and "快照未到" in h
    assert "目前没有任何方向信号通过检验" in h


def test_detail_has_five_sections_in_order():
    h = v2.render_detail_html("2026-09-29", _item(), generated_at="now", index_href="v2_d.html")
    heads = ["① 期权墙总览", "② 期权结构", "③ 期权关键点位", "④ 墙位历史", "⑤ 方向判断（验证中）"]
    pos = [h.index(x) for x in heads]
    assert pos == sorted(pos) and "不作开仓依据" in h and "← 返回品种一览" in h and "≈4088" in h
    md = v2.render_md("2026-09-29", [_item(), _item("missing")], generated_at="now")
    assert "| 2026-09-30 | 季度 | 1 |" in md and "验证中" in md


def test_daily_generates_v2_after_old_report_without_blocking():
    src = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    assert src.index("python3 -m undertow report gold") < src.index("python3 -m undertow report-v2")
    assert "V2_RC != 0 && V2_RC != 3" in src and src.index("alert() {") < src.index("report-v2")



def test_validating_requires_real_frozen_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(v2, "_LEDGER", tmp_path)                                  # 台账里没有这一规则版本的目录
    ok, why = v2.section_allowed(v2.Section("d", "方向", "validating", ("conviction.h1.v1",)))
    assert not ok and "冻结身份" in why


def test_validating_rejects_empty_and_manifest_drift(monkeypatch, tmp_path):
    ok, why = v2.section_allowed(v2.Section("d", "方向", "validating", ()))
    assert not ok and "没有登记任何主张" in why                                    # Codex 032 R6
    import json
    m = tmp_path / "m.json"
    (tmp_path / "f.py").write_text("x")
    m.write_text(json.dumps({"files_sha256": {"f.py": "0" * 64}}))
    monkeypatch.setattr(v2, "_ROOT", tmp_path)
    assert v2._manifest_drift(["m.json"]) == ["f.py"]
    ok, why = v2.section_allowed(v2.Section("d", "方向", "validating", ("conviction.h1.v1",)))
    assert not ok                                                               # 根目录换了：引用文件不在 → 拒绝
