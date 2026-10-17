"""逐根练习页的二次标注题（relabel-v1；用户 2026-10-17：「用你标注的图（遮住未来走势），让我二次标注来看看我们的标注差异。
然后再对比下游未来的走势，判断我和你谁对。」）：算法标注只用截止根及之前、题库可复现、判定纯函数、锁定后不可改、合并与 schema、
页面执行路径（无头 Chrome）。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bar_drill_page as bd  # noqa: E402
from scripts import bar_drill_serve as sv  # noqa: E402

ENGINE = ROOT / "scripts/bar_drill_engine.js"
SYNC = ROOT / "scripts/bar_drill_sync.js"
RELABEL_JS = ROOT / "scripts/bar_drill_relabel.js"
META = {"engine_sha256": "e" * 64, "data_sha256": {"GLD": "d" * 64}}
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _raw(sym="GLD"):
    return bd.load_series(sym)[0]


def _node(js: str):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    code = (f"const Drill = require({json.dumps(str(ENGINE))});\nconst DrillSync = require({json.dumps(str(SYNC))});\n"
            f"const Relabel = require({json.dumps(str(RELABEL_JS))});\n"
            f"const out = (() => {{ {js} }})();\nconsole.log(JSON.stringify(out));")
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── 算法标注与题库 ─────────────────────────────────────────────────────────

def test_annotation_is_prefix_invariant():
    """截止根之后的 K 线乱改或整段截掉，算法标注一个字节都不变（只用截止根及之前）。"""
    raw = _raw()
    i0 = 1500
    base = bd.annotate(raw, i0, 300, False)
    mut = {k: list(v) for k, v in raw.items()}
    for i in range(i0 + 1, i0 + 40):
        for k in "ohlc":
            mut[k][i] *= 1.37
    cut = {k: v[:i0 + 1] for k, v in raw.items()}
    assert bd.annotate(mut, i0, 300, False) == base == bd.annotate(cut, i0, 300, False)
    assert bd.annotate(cut, i0, 300, True) == bd.annotate(raw, i0, 300, True)
    assert all(p["i"] <= p["conf"] <= 299 for p in base["points"])
    assert base["direction"] in ("看涨", "看跌", "不确定") and base["atr"] > 0


def test_annotation_labels_direction_and_live_zones():
    raw = _raw("SLV")
    ann = bd.annotate(raw, 2000, 300, False)
    hs = [p for p in ann["points"] if p["kind"] == "H"]
    ls = [p for p in ann["points"] if p["kind"] == "L"]
    assert hs[0]["label"] == "H" and ls[0]["label"] == "L"
    for seq, up, dn in ((hs, "HH", "LH"), (ls, "HL", "LL")):
        for a, b in zip(seq, seq[1:]):
            assert b["label"] == (up if b["price"] > a["price"] else dn)
    want = "不确定"
    if hs[-1]["price"] > hs[-2]["price"] and ls[-1]["price"] > ls[-2]["price"]:
        want = "看涨"
    elif hs[-1]["price"] < hs[-2]["price"] and ls[-1]["price"] < ls[-2]["price"]:
        want = "看跌"
    assert ann["direction"] == want
    assert ann["invalid"] == {"看涨": ls[-1]["price"], "看跌": hs[-1]["price"]}.get(want)
    C = [b[4] for b in bd.transform(raw, 2000, 300, False)]
    for z in ann["zones"]:                      # 截止时仍有效：启用时在正确一侧、之后没有收盘越过远沿
        after = C[z["active"] + 1:]
        if z["kind"] == "R":
            assert C[z["active"]] < z["lo"] and all(c <= z["hi"] for c in after)
        else:
            assert C[z["active"]] > z["hi"] and all(c >= z["lo"] for c in after)


def test_bank_is_deterministic_and_leaves_future():
    series = {s: _raw(s) for s in ("GLD", "SLV", "AAPL")}
    a, b = bd.relabel_bank(series), bd.relabel_bank(series)
    assert a == b and len(a["items"]) == bd.RELABEL["n"]
    assert sum(it["mirror"] for it in a["items"]) == bd.RELABEL["n"] // 2
    assert len({it["rid"] for it in a["items"]}) == bd.RELABEL["n"]
    for it in a["items"]:
        assert it["left"] - 1 <= it["i0"] <= len(series[it["sym"]]["c"]) - it["right"] - 1
    assert set(a["meta"]["algo_sha256"]) == set(bd.ALGO_FILES)


def test_python_transform_matches_engine_window():
    raw = {k: v[:400] for k, v in _raw().items()}
    out = _node(f"""const raw = {json.dumps(raw)};
      return [Drill.window(raw, {{i0: 350, left: 300, right: 20, mirror: false}}), Drill.window(raw, {{i0: 350, left: 300, right: 20, mirror: true}})];""")
    for w, mirror in zip(out, (False, True)):
        py = bd.transform(raw, 350, 300, mirror, upto=370)
        assert len(w) == len(py) == 320
        assert all(abs(x - y) < 1e-9 for r, p in zip(w, py) for x, y in zip(r[1:5], p[1:5]))


def test_render_embeds_relabel_logic_and_sha():
    import hashlib
    pool = {"meta": {"etf": ["GLD"], "stock": [], "sha256": {"GLD": "d" * 64}},
            "series": {"GLD": {"d": [20000103], "o": [1], "h": [1], "l": [1], "c": [1], "v": [1]}}}
    html = bd.render(pool)
    assert "__RELABEL__" not in html and "const Relabel" in html
    assert pool["meta"]["relabel_sha256"] == hashlib.sha256(RELABEL_JS.read_bytes()).hexdigest()
    assert pool["meta"]["engine_sha256"] == hashlib.sha256(ENGINE.read_bytes()).hexdigest()   # 判定另放一个文件，不改撮合引擎身份


# ── 判定纯函数 ─────────────────────────────────────────────────────────────

def test_judge_rules():
    out = _node("""
      const mk = (c20, lo, hi) => { const B = []; for (let i = 0; i <= 25; i++) B.push([i, 100, 101, 99, 100, 1]);
        B[25] = [25, 100, hi ?? 101, lo ?? 99, c20, 1]; return B; };
      const J = (c, d, inv, lo, hi) => Relabel.judge(mk(c, lo, hi), 5, 2, d, inv, 20, 0.5);
      return [J(102, '看涨'), J(100.9, '看涨'), J(100.9, '不确定'), J(98, '看跌'), J(98, '不确定'), J(102, '看跌'),
              J(102, '看涨', 97, 96.5), J(102, '看涨', 97), J(98, '看跌', 103, null, 104), J(100, '不确定', 95),
              Relabel.judge(mk(100), 10, 2, '看涨', null, 20, 0.5)];""")
    assert [x["ok"] for x in out[:6]] == [True, False, True, True, False, False]   # ≥0.5 方向对才对；<0.5 只有「不确定」对
    assert out[0]["move"] == 1.0 and out[0]["inv_hit"] is None
    assert out[6]["inv_hit"] is True and out[7]["inv_hit"] is False and out[8]["inv_hit"] is True
    assert out[9]["inv_hit"] is None                    # 不确定不看失效位
    assert out[10] is None                              # 未来不足 20 根 → 不判


def test_check_answer_and_diff():
    out = _node("""
      const ca = [Relabel.checkAnswer(null, null, 100), Relabel.checkAnswer('看涨', 101, 100), Relabel.checkAnswer('看跌', 99, 100),
                  Relabel.checkAnswer('不确定', 99, 100), Relabel.checkAnswer('看涨', 99, 100), Relabel.checkAnswer('不确定', null, 100)];
      const claude = {points: [{i: 10, kind: 'H', label: 'HH', price: 110}, {i: 20, kind: 'L', label: 'HL', price: 95}, {i: 3, kind: 'H', label: 'H', price: 108}],
                      zones: [{kind: 'S', lo: 96, hi: 97}, {kind: 'R', lo: 112, hi: 113}]};
      const user = [{type: 'HH', i: 12, price: 110}, {type: 'LL', i: 20, price: 95}, {type: 'LH', i: 4, price: 108},
                    {type: '支撑线', i: 0, price: 97.4}, {type: '压力线', i: 0, price: 120}];
      const d = Relabel.diff(user, claude, 2, 3, 0.25);
      return {ca, pm: d.points.match.map(x => [x.user.type, x.claude.label]), pu: d.points.user_only.map(x => x.type),
              pc: d.points.claude_only.map(x => x.label), lm: d.levels.match.map(x => [x.user.type, x.claude.kind]),
              lu: d.levels.user_only.map(x => x.type), lc: d.levels.claude_only.map(x => x.kind)};""")
    ca = out["ca"]
    assert ca[0] and ca[1] and ca[2] and ca[3] and ca[4] is None and ca[5] is None
    assert out["pm"] == [["LH", "H"], ["HH", "HH"]]     # 按距离贪心；首点 H 与同类任一标签相容；LL 对 HL 标签不同不算一致
    assert out["pu"] == ["LL"] and out["pc"] == ["HL"]
    assert out["lm"] == [["支撑线", "S"]] and out["lu"] == ["压力线"] and out["lc"] == ["R"]   # 97.4 在 S 区上沿 + 0.25·ATR 内


def test_stats():
    out = _node("""return Relabel.stats([
      {user: {dir: '看涨', ok: true, inv_hit: null}, claude: {dir: '看跌', ok: false, inv_hit: false}},
      {user: {dir: '看跌', ok: false, inv_hit: true}, claude: {dir: '看跌', ok: false, inv_hit: null}},
      {user: {dir: '不确定', ok: false, inv_hit: null}, claude: {dir: '看涨', ok: true, inv_hit: false}}]);""")
    assert out == {"n": 3, "user_ok": 1, "claude_ok": 1, "disagree": 2, "disagree_user_ok": 1, "disagree_claude_ok": 1,
                   "user_inv_hit": 1, "user_inv_set": 1}


# ── 合并与 schema ──────────────────────────────────────────────────────────

SHA = "a" * 64
ANS = {"dir": "看涨", "invalid": 95.0, "at": "T"}
RES = {"user": {"dir": "看涨", "ok": True, "inv_hit": False}, "claude": {"dir": "不确定", "ok": False, "inv_hit": None},
       "move": 1.2, "relabel_sha256": "b" * 64, "at": "T"}
MK = {"action": "add", "id": "m1", "type": "HH", "i": 290, "price": 101.5}


def _rl(rid="relabel-v1-1-000", *, marks=None, actions=None, locked=False, revealed=False, answer=None, result=None, item_sha=SHA):
    return {"rid": rid, "item_sha": item_sha, "created_at": "T", "marks": list(marks or []), "actions": list(actions or []),
            "answer": answer, "locked": locked, "revealed": revealed, "result": result, "note": ""}


def _rec(base, questions, **kw):
    return {"version": "bar-drill-v1", "base_revision": base, "page_engine_sha256": META["engine_sha256"],
            "data_sha256": META["data_sha256"], "questions": questions, **kw}


def test_merge_rules():
    out = _node(f"""
      const base = {json.dumps(_rl(marks=[MK]))}, ahead = {{...base, marks: [...base.marks, {{...base.marks[0], id: 'm2'}}]}};
      const locked = {{...ahead, locked: true, answer: {json.dumps(ANS)}}};
      const other = {{...base, marks: [...base.marks, {{...base.marks[0], id: 'm3'}}]}};
      const relock = {{...locked, answer: {{...locked.answer, dir: '看跌'}}}};
      const m = DrillSync.mergeRelabel({{relabel: [other]}}, {{relabel: [ahead]}}, 'T');
      const m2 = DrillSync.mergeRelabel({{relabel: [other], relabel_conflicts: m.relabel_conflicts}}, {{relabel: [ahead], relabel_conflicts: m.relabel_conflicts}}, 'T');
      const add = DrillSync.mergeRelabel({{relabel: [base, {{...base, rid: 'x'}}]}}, {{relabel: [base]}}, 'T');
      return {{rel: [DrillSync.rlRelation(ahead, base), DrillSync.rlRelation(base, ahead), DrillSync.rlRelation(base, locked),
                    DrillSync.rlRelation(relock, locked), DrillSync.rlRelation({{...base, item_sha: 'c'}}, base)],
              div: m.diverged, kept: m.relabel[0].marks.map(x => x.id), copies: m.relabel_conflicts.length, again: m2.relabel_conflicts.length,
              changed2: m2.changed, add: add.relabel.map(r => r.rid), add_changed: add.changed,
              old: DrillSync.mergeRelabel({{questions: []}}, {{questions: []}}, 'T')}};""")
    assert out["rel"] == ["local", "server", "server", "diverged", "diverged"]   # 已锁定的作答不同 = 分叉，不许覆盖
    assert out["div"] == 1 and out["kept"] == ["m1", "m2"] and out["copies"] == 1 and out["again"] == 1 and out["changed2"] is False
    assert out["add"] == ["relabel-v1-1-000", "x"] and out["add_changed"] is True
    assert out["old"] == {"relabel": [], "relabel_conflicts": [], "diverged": 0, "changed": False}   # 旧记录没有 relabel


def test_schema_and_no_regression(tmp_path):
    store = tmp_path / "d" / "bar_drill_v1.json"
    r0 = _rl(marks=[MK], actions=[{"type": "pick_dir", "dir": "看涨", "at": "T"}])
    assert sv.save(store, _rec(0, [], relabel=[r0]), META)["revision"] == 1
    r1 = {**r0, "locked": True, "answer": ANS, "actions": r0["actions"] + [{"type": "lock", "at": "T"}]}
    assert sv.save(store, _rec(1, [], relabel=[r1]), META)["revision"] == 2
    r2 = {**r1, "revealed": True, "result": RES}
    assert sv.save(store, _rec(2, [], relabel=[r2], relabel_cur=r2["rid"]), META)["revision"] == 3
    before = store.read_bytes()
    for bad in ({**r2, "answer": {**ANS, "dir": "看跌"}}, {**r2, "marks": []}, {**r2, "result": {**RES, "move": -1.0}},
                {**r2, "item_sha": "c" * 64}):
        with pytest.raises(sv.Conflict):
            sv.save(store, _rec(3, [], relabel=[bad]), META)
    with pytest.raises(sv.Conflict):
        sv.save(store, _rec(3, [], relabel=[]), META)                     # 删掉已存的二次标注
    assert store.read_bytes() == before
    for bad in ({**r0, "locked": True}, {**r0, "revealed": True}, {**r0, "answer": ANS},
                {**r1, "answer": {**ANS, "dir": "不确定"}}, {**r0, "extra": 1}, {**r0, "item_sha": "x"},
                {**r0, "marks": [{**MK, "type": "随便"}]}, {**r1, "revealed": True}):
        with pytest.raises(ValueError):
            sv.validate(_rec(3, [], relabel=[bad]), META)
    with pytest.raises(ValueError):
        sv.validate(_rec(3, [], relabel=[r0, r0]), META)                  # rid 重复
    sv.validate(_rec(0, []), META)                                        # 旧记录没有 relabel 照常可读


# ── 页面执行路径（无头 Chrome，file:// 只存浏览器）──────────────────────────────

def test_page_relabel_flow_blind_lock_reveal(tmp_path):
    """先盲标（算法标注不显示）→ 未选方向不能锁 → 锁定后标注/方向不能再改、才显示差异 → 揭示写判定；逐根题不受影响。"""
    if not CHROME.exists():
        pytest.skip("没有 Chrome")
    n = 400
    c = [100 + (i % 37) * 0.8 - (i % 11) * 0.5 for i in range(n)]
    raw = {"d": [20000103 + i for i in range(n)], "o": c, "h": [x + 1 for x in c], "l": [x - 1 for x in c], "c": c, "v": [1] * n}
    pool = {"meta": {"etf": ["GLD"], "stock": [], "sha256": {"GLD": "d" * 64}}, "series": {"GLD": raw},
            "relabel": bd.relabel_bank({"GLD": raw}, {**bd.RELABEL, "n": 2})}
    html = bd.render(pool)
    probe = """<script>
      const out = {};
      $("kind").value = "relabel"; $("kind").onchange();
      const it = RLB.items[0]; out.cur = DB.relabel_cur === it.rid;
      out.claude_hidden = !document.querySelector("#chart svg").innerHTML.includes("算法");
      out.diff_hidden = $("rldiff").hidden;
      rlLock(); out.lock_without_dir = !!(rlRec(it.rid) || {}).locked;
      mode = "HH"; const v = rlView(), bars = barsOf(v);
      click({clientX: 0, clientY: 0}, v, bars);        // locate 取不到坐标也会落在可见范围内
      rlPickDir("看涨"); rlLock();
      const r = rlRec(it.rid); out.locked = r.locked; out.answer = r.answer.dir; out.marks = r.marks.length;
      out.claude_shown = document.querySelector("#chart svg").innerHTML.includes("算法") || it.claude.points.length === 0;
      out.diff_shown = !$("rldiff").hidden;
      rlPickDir("看跌"); mode = "LL"; click({clientX: 0, clientY: 0}, rlView(), bars);
      out.after_lock_same = r.answer.dir === "看涨" && rlRec(it.rid).marks.length === out.marks && rlDraft(r).dir === "看涨";
      out.future_hidden = rlView().cursor === it.left - 1;
      rlReveal(); const r2 = rlRec(it.rid);
      out.revealed = r2.revealed; out.result = r2.result; out.cursor = rlView().cursor; out.reveal_card = !$("reveal").hidden;
      out.stats = $("rlover").innerHTML.includes("已揭示 1");
      $("kind").value = "drill"; $("kind").onchange(); out.drill_rows = !$("g_next").hidden && $("rlbar").hidden;
      out.questions = DB.questions.length;
      document.body.setAttribute("data-probe", JSON.stringify(out));
    </script>"""
    html = html.replace("</body></html>", probe + "</body></html>")
    f = tmp_path / "page.html"
    f.write_text(html, "utf-8")
    r = subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'prof'}", "--dump-dom", f.as_uri()],
                       capture_output=True, text=True, timeout=90)
    import html as _h
    import re
    m = re.search(r'data-probe="([^"]*)"', r.stdout)
    assert m, r.stdout[-500:] + r.stderr[-500:]
    out = json.loads(_h.unescape(m.group(1)))
    assert out["cur"] and out["claude_hidden"] and out["diff_hidden"] and not out["lock_without_dir"]
    assert out["locked"] and out["answer"] == "看涨" and out["marks"] == 1 and out["claude_shown"] and out["diff_shown"]
    assert out["after_lock_same"] and out["future_hidden"]
    assert out["revealed"] and out["cursor"] == 319 and out["reveal_card"] and out["stats"]
    res = out["result"]
    assert res["user"]["dir"] == "看涨" and type(res["user"]["ok"]) is bool and len(res["relabel_sha256"]) == 64
    assert out["drill_rows"] and out["questions"] == 0
