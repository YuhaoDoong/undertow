"""逐根回测练习（bar-drill-v1）：数据打包与原始日线一致、只到 2015；撮合引擎（node 跑同一份 JS）的开盘成交、止损止盈、跳空、
同根双触及、反手、R 计算；镜像口径与 ps_trigger_v2.mirror 一致；出题可复现且窗口完整。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bar_drill_page as bd  # noqa: E402
from scripts import ps_explore as pe  # noqa: E402

ENGINE = ROOT / "scripts/bar_drill_engine.js"


def test_series_match_inputs_and_stop_at_2015():
    s, _ = bd.load_series("GLD")
    bars = pe.parse(json.loads((bd.SRC / "inputs/GLD.json").read_text("utf-8")))[0]
    assert len(s["c"]) == len(bars)
    assert all(abs(c - b[4]) / b[4] < 1e-5 for c, b in zip(s["c"], bars))
    assert s["d"][-1] <= 20151231 and s["d"] == sorted(s["d"])


def test_render_fills_placeholders():
    pool = {"meta": {"etf": ["GLD"], "stock": [], "sha256": {}}, "series": {"GLD": {"d": [20000103], "o": [1], "h": [1], "l": [1], "c": [1], "v": [1]}}}
    html = bd.render(pool)
    assert "__ENGINE__" not in html and "__DATA__" not in html and "const Drill" in html


def _node(js: str):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    code = f"const Drill = require({json.dumps(str(ENGINE))});\nconst out = (() => {{ {js} }})();\nconsole.log(JSON.stringify(out));"
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


BARS = """const B = [[0,100,101,99,100,1],[1,100,102,99.5,101,1],[2,101,103,100,102,1],[3,102,102.5,97,98,1],[4,98,99,96,97,1],[5,95,96,94,95,1]];
const st = {cursor: 0, max_cursor: 0, pos: null, pending: null, trades: []};"""


def test_open_fill_and_intrabar_stop():
    out = _node(BARS + """
      st.pending = {type: "long"};
      Drill.step(st, B);                          // 第 1 根开盘 100 成交
      Drill.setLevel(st, B, "stop", 99);
      Drill.step(st, B); Drill.step(st, B);       // 第 3 根低 97 ≤ 99 → 止损 99
      return st.trades;""")
    t = out[0]
    assert t["entry"] == 100 and t["entry_i"] == 1 and t["exit"] == 99 and t["exit_i"] == 3 and t["reason"] == "止损"
    assert abs(t["r"] + 1) < 1e-9 and abs(t["ret_pct"] + 1) < 1e-9


def test_gap_through_stop_fills_at_open_and_both_hit_counts_stop():
    out = _node(BARS + """
      st.cursor = 3; st.pos = {side: 1, entry: 98, entry_i: 3, stop: 96.5, stop0: 96.5, target: null};
      Drill.step(st, B);                          // 第 4 根开 98 低 96 → 止损 96.5
      const a = st.trades[0];
      st.cursor = 4; st.pos = {side: 1, entry: 97, entry_i: 4, stop: 96, stop0: 96, target: 99};
      Drill.step(st, B);                          // 第 5 根开 95 ≤ 96 → 跳空按开盘 95
      const b = st.trades[1];
      const B2 = [[0,100,100,100,100,1],[1,100,103,97,100,1]];
      const s2 = {cursor: 0, max_cursor: 0, pos: {side: 1, entry: 100, entry_i: 0, stop: 98, stop0: 98, target: 102}, pending: null, trades: []};
      Drill.step(s2, B2);
      return [a, b, s2.trades[0]];""")
    assert out[0]["exit"] == 96.5 and out[0]["reason"] == "止损"
    assert out[1]["exit"] == 95 and "跳空" in out[1]["reason"]
    assert out[2]["exit"] == 98 and "按止损" in out[2]["reason"]


def test_short_reverse_and_target():
    out = _node(BARS + """
      st.pending = {type: "long"}; Drill.step(st, B);          // 第 1 根开 100 做多
      st.pending = {type: "short", target: 96}; Drill.step(st, B);  // 第 2 根开 101：先平多（反手）再开空，止盈 96
      Drill.step(st, B); Drill.step(st, B);                     // 第 4 根低 96 ≤ 96 → 止盈
      return st.trades;""")
    assert out[0]["reason"] == "反手" and out[0]["exit"] == 101 and out[0]["side"] == 1
    assert out[1]["side"] == -1 and out[1]["entry"] == 101 and out[1]["exit"] == 96 and out[1]["reason"] == "止盈"
    assert out[1]["r"] is None and out[1]["ret_pct"] > 0


def test_set_level_rejects_wrong_side():
    out = _node(BARS + """
      const a = Drill.setLevel(st, B, "stop", 99);              // 没仓位
      st.pos = {side: 1, entry: 100, entry_i: 0, stop: null, stop0: null, target: null};
      const b = Drill.setLevel(st, B, "stop", 101);             // 多单止损在现价上方
      const c = Drill.setLevel(st, B, "stop", 98);
      return [a, b, c, st.pos.stop0];""")
    assert out[0] and out[1] and out[2] is None and out[3] == 98


def test_mirror_matches_ps_mirror_and_pick_is_reproducible():
    from scripts.ps_trigger_v2 import mirror
    s, _ = bd.load_series("SLV")
    out = _node(f"""
      const raw = {json.dumps({k: s[k][:900] for k in "dohlcv"})};
      const pool = {{SLV: {{n: raw.c.length}}}};
      const opts = {{left: 300, right: 150, mirrorProb: 1, syms: ["SLV"]}};
      const q = Drill.pick(pool, 12345, opts), q2 = Drill.pick(pool, 12345, opts);
      return {{q, same: JSON.stringify(q) === JSON.stringify(q2), w: Drill.window(raw, q)}};""")
    q, w = out["q"], out["w"]
    assert out["same"] and q["mirror"] is True and len(w) == 450 and w[299][0] == 0 and abs(w[299][4] - 100) < 1e-9
    i0 = q["i0"]
    f = 100 / s["c"][i0]
    seg = [(None, s["o"][j] * f, s["h"][j] * f, s["l"][j] * f, s["c"][j] * f) for j in range(i0, i0 + 150)]
    ref = mirror(seg)                                            # K = 首根收盘² = 100²
    for a, b in zip(w[299:], ref):
        assert all(abs(x - y) < 1e-6 * y for x, y in zip(a[1:5], b[1:5]))
