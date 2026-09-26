"""Codex 008 G02：私有档案损坏不得当空档，更不得被下一次写入覆盖。全部用临时目录合成文件。"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from undertow.soul import journal as jn
from undertow.soul import plan as pl
from undertow.soul import profile as pf
from undertow.soul._store import PrivateStoreError

BAD = b'{"entries": [ {"date": "2026-09-01", "title": "x"'        # 截断的 JSON


def _corrupt(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(BAD)
    return p


@pytest.mark.parametrize("loader,name", [(jn.load_journal, "journal.json"), (jn.load_theses, "journal.json"),
                                         (pl.load_plans, "plans.json"), (pf.load_profile, "profile.json")])
def test_corrupt_load_raises_and_quarantines(tmp_path, loader, name):
    p = _corrupt(tmp_path, name)
    with pytest.raises(PrivateStoreError, match="已损坏"):
        loader(p)
    assert p.read_bytes() == BAD, "原件原字节保留"
    assert list(tmp_path.glob(f"{name}.corrupt-*")), "同目录隔离副本"


def test_absent_is_empty_not_error(tmp_path):
    assert jn.load_journal(tmp_path / "j.json") == [] and pl.load_plans(tmp_path / "p.json") == []
    assert pf.load_profile(tmp_path / "x.json") is None


def test_wrong_structure_raises(tmp_path):
    p = tmp_path / "plans.json"
    p.write_text(json.dumps({"plans": [{"id": "a", "underlying": "SLV", "structure": "x", "direction": "above"}]}))
    with pytest.raises(PrivateStoreError, match="结构不符"):
        pl.load_plans(p)                                    # 缺触发价：旧实现默认 0 →「涨到」永远已触发
    p.write_text("[1, 2]")
    with pytest.raises(PrivateStoreError, match="顶层类型"):
        pl.load_plans(p)


@pytest.mark.parametrize("saver,name,arg", [
    (lambda path: jn.save_journal([], path, theses=[]), "journal.json", None),
    (lambda path: jn.save_journal([], path), "journal.json", None),
    (lambda path: pl.save_plans([], path), "plans.json", None),
])
def test_save_refuses_to_overwrite_corrupt(tmp_path, saver, name, arg):
    p = _corrupt(tmp_path, name)
    with pytest.raises(PrivateStoreError):
        saver(p)
    assert p.read_bytes() == BAD


def test_save_roundtrip_atomic(tmp_path):
    p = tmp_path / "journal.json"
    e = jn.JournalEntry(date="2026-09-28", title="t")
    jn.save_journal([e], p, theses=[])
    assert jn.load_journal(p)[0].title == "t"
    jn.save_journal([e], p)                                 # theses=None：保留文件里的 theses
    assert json.loads(p.read_text())["theses"] == []
    assert not list(tmp_path.glob("*.tmp"))


def test_journal_capture_refuses_on_corrupt_history(tmp_path, monkeypatch, capsys):
    """端到端：旧实现读到空 → 加入当天 → 保存，把整本坏历史覆盖成一天。"""
    from undertow import cli
    from undertow.collect import longbridge_account as lb
    p = _corrupt(tmp_path, "journal.json")
    monkeypatch.setattr(jn, "DEFAULT_PATH", p)
    monkeypatch.setattr(lb, "fetch_today_executions", lambda: [{"symbol": "SLV.US"}])
    monkeypatch.setattr(lb, "fetch_cash_flow", lambda start=None, end=None: [])
    rc = cli.cmd_journal(argparse.Namespace(capture=True, theses=False, date=None))
    assert rc == 2 and p.read_bytes() == BAD and "未做任何写入" in capsys.readouterr().err


# —— 纪律核查：资金未知 / 净资产 0 / 风险资金未知 ——
def _prof(**lim):
    return pf.SoulProfile(schema=1, updated="", owner="", phase="", north_star="",
                          rules=[pf.Rule(id="r", severity="铁律", text="t", why="w")] if hasattr(pf, "Rule") else [],
                          limits=pf.Limits(**lim))


def _review(caps):
    combos = [NS(capital_at_risk=c, label=f"c{i}", net_credit=None, max_loss=c, qty=1, legs=[])
              for i, c in enumerate(caps)]
    return NS(groups=[NS(combos=combos, display_name="白银", underlying="SLV")])


def test_capital_unknown_is_not_pass():
    v = pf.check_against_profile(_review([100.0]), None, _prof(max_concentration_pct=20))
    assert v and v[0].severity == "未完成核查" and v[0].rule_id == "capital_unknown"


def test_zero_net_assets_flags_any_risk():
    v = pf.check_against_profile(_review([10.0]), NS(net_assets=0.0), _prof(max_concentration_pct=20))
    assert any(x.severity == "违反铁律" and "净资产 ≤0" in x.detail for x in v)


def test_unknown_member_risk_is_incomplete_not_pass():
    v = pf.check_against_profile(_review([10.0, None]), NS(net_assets=1000.0),
                                 _prof(max_concentration_pct=20, max_loss_per_trade_pct=20))
    kinds = {(x.severity, x.rule_id) for x in v}
    assert ("未完成核查", "max_concentration_pct") in kinds and ("未完成核查", "max_loss_per_trade_pct") in kinds


def test_consult_packet_marks_discipline_unchecked():
    from undertow.consult.packet import render_prompt
    base = {"guidance": [], "asof": "2026-09-28", "mode": "review", "instruments": {},
            "portfolio": {"headline": "无", "groups": []}, "healthcheck": [], "question": ""}
    for pkt in ({**base, "soul": None, "soul_status": "error: PrivateStoreError: 坏档"},
                {**base, "soul": None, "soul_status": "absent"}):
        try:
            txt = render_prompt(pkt)
        except KeyError as e:                       # 包里其余必填字段：按报错补齐
            pytest.fail(f"合成包缺字段 {e}，请补齐后再断言")
        assert "【纪律层：未完成核查】" in txt


def test_consult_source_wires_status():
    src = (Path(__file__).resolve().parents[1] / "undertow" / "consult" / "packet.py").read_text("utf-8")
    assert "【纪律层：未完成核查】" in src and '"soul_status"' in src
