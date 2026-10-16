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


def _tiny_pool():
    return {"meta": {"etf": ["GLD"], "stock": [], "sha256": {"GLD": "d" * 64}},
            "series": {"GLD": {"d": [20000103], "o": [1], "h": [1], "l": [1], "c": [1], "v": [1]}}}


def test_render_fills_placeholders_and_binds_engine_sha():
    import hashlib
    pool = _tiny_pool()
    html = bd.render(pool)
    assert "__ENGINE__" not in html and "__SYNC__" not in html and "__DATA__" not in html
    assert "const Drill" in html and "const DrillSync" in html
    assert pool["meta"]["engine_sha256"] == hashlib.sha256(ENGINE.read_bytes()).hexdigest()
    assert pool["meta"]["engine_sha256"] in html                     # 页面据此给新题记引擎版本
    assert html.count("<head>") == 1                                 # 服务在其后注入写入凭据


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


def test_finish_early_exits_next_open_else_admin_mark():
    """Codex 053 E：主动结束也是看完当前收盘才决定 → 下一根开盘平（正常成交）；没有下一根 / 自然走到头 → 行政盯市单列。"""
    out = _node("""
      const B = [[0,100,101,99,100,1],[1,110,111,109,110,1],[2,80,81,79,80,1]];
      const a = {cursor: 0, max_cursor: 0, pos: null, pending: {type: "long"}, trades: []};
      Drill.step(a, B); const ta = Drill.finish(a, B, true);              // 当前收 110、下一根开 80 → 记 80，不是 110
      const b = {cursor: 0, max_cursor: 0, pos: null, pending: {type: "long"}, trades: []};
      Drill.step(b, B); Drill.step(b, B); const tb = Drill.finish(b, B, true);   // 已在最后一根 → 行政盯市
      const c = {cursor: 0, max_cursor: 0, pos: null, pending: {type: "long"}, trades: []};
      Drill.step(c, B); Drill.step(c, B); const tc = Drill.finish(c, B, false);  // 自然走到头
      const d = {cursor: 0, max_cursor: 0, pos: null, pending: {type: "long"}, trades: []};
      const td = Drill.finish(d, B, true);                                // 空仓挂着开仓单 → 作废，不成交
      return {ta, a_cursor: a.cursor, tb, tc, td, d_pending: d.pending, d_trades: d.trades.length, done: [a.done, b.done, c.done, d.done],
              st: Drill.stats([...a.trades, ...b.trades]), version: Drill.VERSION};""")
    assert out["version"] == "bar-drill-engine-2"
    assert out["ta"]["exit"] == 80 and out["ta"]["exit_i"] == 2 and out["ta"]["exit_kind"] == "fill" and out["a_cursor"] == 2
    assert out["tb"]["exit"] == 80 and out["tb"]["exit_kind"] == "admin" and "行政" in out["tb"]["reason"]
    assert out["tc"]["exit_kind"] == "admin" and "行政" in out["tc"]["reason"]
    assert out["td"] is None and out["d_pending"] is None and out["d_trades"] == 0 and all(out["done"])
    st = out["st"]
    assert st["n"] == 1 and st["n_admin"] == 1 and abs(st["sum_pct"] - (80 / 110 - 1) * 100) < 1e-9   # 行政平仓不进收益之和
    assert abs(st["sum_pct_admin"] - (80 / 110 - 1) * 100) < 1e-9


def test_late_stop_r_kept_apart_from_pre_entry_stop():
    out = _node(BARS + """
      st.pending = {type: "long", stop: 99}; Drill.step(st, B);          // 开仓单带止损 → 事前风险
      st.pending = {type: "exit"}; Drill.step(st, B);                       // 第 2 根开 101 平
      st.pending = {type: "long"}; Drill.step(st, B);                       // 第 3 根开 102 无止损成交
      Drill.setLevel(st, B, "stop", 97.5);                                  // 成交后才设第一个止损 → 迟设
      Drill.step(st, B);                                                    // 第 4 根低 96 → 止损 97.5
      return {t: st.trades, s: Drill.stats(st.trades)};""")
    t, s = out["t"], out["s"]
    assert t[0]["stop0_late"] is False and abs(t[0]["r"] - 1) < 1e-9
    assert t[1]["stop0_late"] is True and t[1]["exit"] == 97.5 and abs(t[1]["r"] + 1) < 1e-9
    assert s["n"] == 2 and s["n_r"] == 1 and abs(s["sum_r"] - 1) < 1e-9 and s["n_r_late"] == 1 and abs(s["sum_r_late"] + 1) < 1e-9


def test_inverse_mirror_is_not_symmetric_in_simple_return_or_r():
    """页面文案依据：逆数镜像下同一路径的反向交易，简单收益与 R 都比原始小（不能把原始/镜像差直接读成方向偏好）。"""
    out = _node("""
      const tr = (side, entry, exit, stop) => Drill.closeTrade({pos: {side, entry, entry_i: 0, stop0: stop, stop0_late: false}, trades: []}, exit, 1, "x");
      return [tr(1, 100, 110, 95), tr(-1, 100, 10000 / 110, 10000 / 95)];""")
    raw, mir = out
    assert abs(raw["ret_pct"] - 10) < 1e-9 and abs(raw["r"] - 2) < 1e-9
    assert abs(mir["ret_pct"] - 100 / 11) < 1e-9 and abs(mir["r"] - 1.727272727) < 1e-6


def test_page_text_drops_direction_bias_claim():
    page = (ROOT / "scripts/bar_drill_page.html").read_text("utf-8")
    assert "说明可能带着方向偏好" not in page and "按当前收盘平仓" not in page
    assert "不能直接当作方向偏好" in page and "简单收益之和" in page and "行政平仓" in page and "迟设" in page


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



# ---------------------------------------------------------------- 本机服务（Codex 053 B）：全部用临时目录，不碰 data/soul/drill/
import http.client  # noqa: E402
import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

from scripts import bar_drill_serve as sv  # noqa: E402

SYNC = ROOT / "scripts/bar_drill_sync.js"
TOKEN = "tok_test-123"
META = {"engine_sha256": "e" * 64, "data_sha256": {"GLD": "d" * 64}}
PAGE = b"<!doctype html><html><head><title>t</title></head><body>page</body></html>"


@pytest.fixture()
def drill(tmp_path):
    store = tmp_path / "drill" / "bar_drill_v1.json"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), sv.make_handler(PAGE, store, token=TOKEN, page_meta=META))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], store
    srv.shutdown()
    srv.server_close()


def _req(port, method, path, obj=None, *, host=None, origin="same", ctype="application/json", token=TOKEN, raw=None, extra=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    h = host or f"127.0.0.1:{port}"
    c.putheader("Host", h)
    if origin == "same":
        origin = "http://" + h if method == "POST" else None
    if origin:
        c.putheader("Origin", origin)
    if token:
        c.putheader("X-Drill-Token", token)
    for k, v in (extra or {}).items():
        c.putheader(k, v)
    body = raw if raw is not None else (None if obj is None else json.dumps(obj).encode())
    if body is not None:
        if ctype:
            c.putheader("Content-Type", ctype)
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, (json.loads(b) if (r.getheader("Content-Type") or "").startswith("application/json") else b)


def _rec(base, questions, **kw):
    return {"version": "bar-drill-v1", "base_revision": base, "page_engine_sha256": META["engine_sha256"],
            "data_sha256": META["data_sha256"], "questions": questions, **kw}


def _q(seed, cursor, *, qid=1, trades=(), actions=None, done=False):
    acts = actions if actions is not None else [{"type": "next", "as_of": k} for k in range(cursor)]
    return {"id": qid, "seed": seed, "sym": "GLD", "i0": 400, "mirror": False, "left": 300, "right": 150,
            "cursor": cursor, "max_cursor": cursor, "trades": list(trades), "actions": acts, "marks": [], "done": done, "note": ""}


TRADE = {"side": 1, "entry": 100, "entry_i": 3, "exit": 104, "exit_i": 20, "reason": "止盈", "stop0": 98, "ret_pct": 4.0, "r": 2.0}


def test_serve_roundtrip_guard_and_quarantine(drill):
    port, store = drill
    code, body = _req(port, "GET", "/")
    assert code == 200 and f'<meta name="drill-token" content="{TOKEN}">'.encode() in body and b"page" in body
    code, body = _req(port, "GET", "/records")
    assert code == 404 and body["revision"] == 0
    qs = [_q(11, 3), _q(22, 5, qid=2)]
    code, body = _req(port, "POST", "/records", _rec(0, qs))
    assert code == 200 and body["questions"] == 2 and body["revision"] == 1
    code, got = _req(port, "GET", "/records")
    assert got["questions"] == qs and got["revision"] == 1 and "base_revision" not in got
    # 空浏览器提交会丢题 → 拒绝，文件不变
    before = store.read_bytes()
    code, body = _req(port, "POST", "/records", _rec(1, []))
    assert code == 409 and store.read_bytes() == before
    # 加题可以；当天第一次覆盖前留了备份
    assert _req(port, "POST", "/records", _rec(1, qs + [_q(33, 0, qid=3)]))[0] == 200
    assert list((store.parent / "history").glob("bar_drill_v1_*.json"))
    assert _req(port, "POST", "/records", {**_rec(2, []), "version": "x"})[0] == 400
    # 损坏 → 隔离保留 + 恢复未决，报冲突，不当成空记录
    store.write_text("{坏", "utf-8")
    code, body = _req(port, "GET", "/records")
    assert code == 409 and body["recovery_pending"] is True and "--recovery-ack" in body["error"]
    assert not store.exists() and list(store.parent.glob("bar_drill_v1.json.corrupt-*"))
    # 原版漏洞：隔离后原路径空了，下一次空提交被当成首次记录。现在明确阻断，且不建文件
    for _ in range(2):
        code, body = _req(port, "POST", "/records", _rec(0, []))
        assert code == 409 and body["recovery_pending"] is True and not store.exists()
        assert _req(port, "GET", "/records")[0] == 409
    # 用户明确确认后才放行；隔离件原样保留，确认留痕
    assert "已确认" in sv.recovery_ack(store)
    assert _req(port, "POST", "/records", _rec(0, []))[0] == 200
    assert list(store.parent.glob("bar_drill_v1.json.corrupt-*")) and (store.parent / "recovery_log.jsonl").exists()


def test_same_seed_stale_snapshot_cannot_roll_back_progress(drill):
    """Codex 053 复现：同 seed 已存 cursor30 + 1 笔成交，旧 cursor3 + 空成交的快照不得覆盖。"""
    port, store = drill
    old_snap = _q(11, 3)
    assert _req(port, "POST", "/records", _rec(0, [old_snap]))[0] == 200            # 第 1 版：cursor 3
    new = _q(11, 30, trades=[TRADE])
    assert _req(port, "POST", "/records", _rec(1, [new]))[0] == 200                 # 第 2 版：cursor 30，1 笔成交
    saved = store.read_bytes()
    # ① 迟到的旧标签页（基于第 1 版）→ 版本冲突
    code, body = _req(port, "POST", "/records", _rec(1, [old_snap]))
    assert code == 409 and body["conflict"] is True and body["revision"] == 2 and store.read_bytes() == saved
    # ② 即便版本号对上，进度/成交/动作倒退也拒绝
    code, body = _req(port, "POST", "/records", _rec(2, [old_snap]))
    assert code == 409 and any("cursor" in x for x in body["regress"]) and any("trades" in x for x in body["regress"])
    assert store.read_bytes() == saved
    # ③ 已结束不能改回未结束；成交被改写（不是追加）也拒绝
    assert _req(port, "POST", "/records", _rec(2, [{**new, "done": True}]))[0] == 200
    assert _req(port, "POST", "/records", _rec(3, [new]))[0] == 409
    assert _req(port, "POST", "/records", _rec(3, [{**new, "done": True, "trades": [{**TRADE, "exit": 999}]}]))[0] == 409
    assert _req(port, "POST", "/records", _rec(3, [{**new, "done": True, "sym": "SLV"}]))[0] == 409   # 题目身份不能改
    assert json.loads(store.read_text("utf-8"))["questions"][0]["trades"] == [TRADE]


def test_concurrent_writes_one_wins_no_tmp_left(drill):
    port, store = drill
    assert _req(port, "POST", "/records", _rec(0, [_q(1, 0)]))[0] == 200
    codes = []
    def go(k):
        codes.append(_req(port, "POST", "/records", _rec(1, [_q(1, 0), _q(100 + k, 0, qid=2)]))[0])
    ts = [threading.Thread(target=go, args=(k,)) for k in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200] + [409] * 11                       # 同一个 base 只有一个能写成
    d = json.loads(store.read_text("utf-8"))
    assert d["revision"] == 2 and len(d["questions"]) == 2
    assert not list(store.parent.glob("*.tmp"))                        # 唯一临时文件，没有残留


def _merge(local, server):
    return _node_sync(f"return DrillSync.merge({json.dumps(local)}, {json.dumps(server)}, 'T');")


def _node_sync(js):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    code = f"const DrillSync = require({json.dumps(str(SYNC))});\nconst out = (() => {{ {js} }})();\nconsole.log(JSON.stringify(out));"
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_two_tabs_conflict_then_merge_keeps_every_action(drill):
    """双标签页：后到的被 409；合并后两边的动作都在；同题分叉时文件那份保留、本页那份原样存档。"""
    port, store = drill
    a1, a2, b2 = {"type": "long", "as_of": 300}, {"type": "next", "as_of": 300}, {"type": "short", "as_of": 300}
    qa = _q(1, 300, actions=[a1])
    assert _req(port, "POST", "/records", _rec(0, [qa]))[0] == 200                         # 两个标签页都基于第 1 版
    tab_b = [qa, _q(2, 299, qid=2, actions=[])]
    assert _req(port, "POST", "/records", _rec(1, tab_b))[0] == 200                       # B 先写成第 2 版
    tab_a = [{**qa, "cursor": 301, "max_cursor": 301, "actions": [a1, a2]}]
    assert _req(port, "POST", "/records", _rec(1, tab_a))[0] == 409                       # A 迟到 → 拒绝
    srv = _req(port, "GET", "/records")[1]
    m = _merge({"questions": tab_a}, srv)
    assert m["diverged"] == 0 and m["changed"] and [q["seed"] for q in m["questions"]] == [1, 2]
    assert m["questions"][0]["actions"] == [a1, a2]                                        # A 的新动作保住
    assert _req(port, "POST", "/records", _rec(srv["revision"], m["questions"], conflict_copies=m["conflict_copies"]))[0] == 200
    # B 仍停在 [a1]，又往下做了 b2：与文件里的 [a1, a2] 分叉
    srv = _req(port, "GET", "/records")[1]
    tab_b2 = [{**qa, "cursor": 301, "max_cursor": 301, "actions": [a1, b2]}, tab_b[1]]
    m = _merge({"questions": tab_b2}, srv)
    assert m["diverged"] == 1 and m["questions"][0]["actions"] == [a1, a2]
    assert m["conflict_copies"][0]["question"]["actions"] == [a1, b2]
    assert _req(port, "POST", "/records", _rec(srv["revision"], m["questions"], conflict_copies=m["conflict_copies"]))[0] == 200
    d = json.loads(store.read_text("utf-8"))
    assert d["questions"][0]["actions"] == [a1, a2] and d["conflict_copies"][0]["question"]["actions"] == [a1, b2]
    # 冲突副本只许追加
    assert _req(port, "POST", "/records", _rec(d["revision"], d["questions"], conflict_copies=[]))[0] == 409


def test_merge_relations():
    out = _node_sync("""
      const base = {seed: 1, id: 1, cursor: 5, max_cursor: 5, actions: [{a: 1}], trades: [], marks: []};
      const ahead = {...base, cursor: 6, max_cursor: 6, actions: [{a: 1}, {a: 2}]};
      const fork = {...base, cursor: 6, max_cursor: 6, actions: [{a: 1}, {a: 3}]};
      return [DrillSync.relation(ahead, base), DrillSync.relation(base, ahead), DrillSync.relation(fork, ahead),
              DrillSync.relation(base, base), DrillSync.relation({...base, done: false}, {...base, done: true}),
              DrillSync.merge({questions: [{...base, id: 7, seed: 9}]}, {questions: [base]}, 'T').questions.map(q => [q.seed, q.id])];""")
    assert out[:5] == ["local", "server", "diverged", "local", "server"]
    assert out[5] == [[1, 1], [9, 2]]                                    # 本机独有的题接在后面、重新编号


def test_foreign_or_malformed_requests_rejected(drill, monkeypatch):
    """127.0.0.1 不是鉴权：非本机 Host、非同源、缺凭据、错误内容类型、坏 JSON/结构、旧页面都拒绝，且什么都不写。"""
    port, store = drill
    ok = _rec(0, [_q(1, 0)])
    assert _req(port, "GET", "/", host="evil.example")[0] == 403
    assert _req(port, "GET", "/records", host="evil.example:8788")[0] == 403
    assert _req(port, "POST", "/records", ok, host="evil.example:8788")[0] == 403          # DNS 重绑定
    assert _req(port, "POST", "/records", ok, origin="http://evil.example")[0] == 403
    assert _req(port, "POST", "/records", ok, origin=None)[0] == 403                        # 写入必须带同源 Origin
    assert _req(port, "POST", "/records", ok, origin=f"http://localhost:{port}")[0] == 403  # 与 Host 不同源
    assert _req(port, "GET", "/records", origin="http://evil.example")[0] == 403
    assert _req(port, "POST", "/records", ok, extra={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert _req(port, "POST", "/records", ok, token=None)[0] == 403
    assert _req(port, "POST", "/records", ok, token="wrong")[0] == 403
    assert _req(port, "GET", "/records", token=None)[0] == 403
    assert _req(port, "POST", "/records", ok, ctype="text/plain")[0] == 415                 # 免预检的简单请求
    assert _req(port, "POST", "/records", ok, ctype=None)[0] == 415
    assert _req(port, "POST", "/records", raw=b"{not json")[0] == 400
    assert _req(port, "POST", "/records", raw="[1]".encode())[0] == 400
    assert _req(port, "POST", "/records", {**ok, "evil": 1})[0] == 400
    assert _req(port, "POST", "/records", {k: v for k, v in ok.items() if k != "base_revision"})[0] == 400
    assert _req(port, "POST", "/records", _rec(0, [{**_q(1, 0), "seed": "1"}]))[0] == 400
    assert _req(port, "POST", "/records", _rec(0, [_q(1, 0), _q(1, 0, qid=2)]))[0] == 400   # 种子重复
    assert _req(port, "POST", "/records", _rec(0, [{**_q(1, 0), "actions": {}}]))[0] == 400
    code, body = _req(port, "POST", "/records", {**ok, "page_engine_sha256": "0" * 64})
    assert code == 409 and body["stale_page"] is True                                      # 旧引擎页面不得写
    assert _req(port, "POST", "/records", {**ok, "data_sha256": {"GLD": "0" * 64}})[0] == 409
    monkeypatch.setattr(sv, "MAX_BODY", 100)
    assert _req(port, "POST", "/records", _rec(0, [_q(1, 50)]))[0] == 413
    assert not store.exists()
    monkeypatch.undo()
    code, body = _req(port, "POST", "/records", ok, host=f"localhost:{port}")            # 合法本机流程（localhost 名）
    assert code == 200 and body["revision"] == 1


def test_recover_from_backup_never_overwrites(tmp_path):
    store = tmp_path / "bar_drill_v1.json"
    sv.save(store, _rec(0, [_q(1, 4)]), META)
    backup = tmp_path / "backup.json"
    backup.write_bytes(store.read_bytes())
    store.write_text("坏", "utf-8")
    with pytest.raises(sv.RecoveryPending):
        sv.load(store)
    with pytest.raises(sv.RecoveryPending):
        sv.save(store, _rec(0, []), META)
    st = sv.recovery_status(store)
    assert st["pending"] and st["quarantined"] and not st["store_exists"]
    assert "恢复 1 题" in sv.recover_from(store, backup)
    d = sv.load(store)
    assert d["questions"][0]["cursor"] == 4 and d["revision"] == 2
    with pytest.raises(ValueError):
        sv.recover_from(store, backup)                                  # 原路径已有文件 → 不覆盖
    assert sv.save(store, _rec(2, [_q(1, 5)]), META)["revision"] == 3
    assert "什么也没做" in sv.recovery_ack(store)


def test_real_page_template_accepts_token_injection():
    sv.make_handler(bd.render(_tiny_pool()).encode("utf-8"), Path("/nonexistent/x.json"), token=TOKEN, page_meta=META)


# ---- Codex 054 B1：合并后题号重排、本机冲突副本、首次写入失败后再合并 ----

def test_bars_cache_follows_window_not_question_id():
    """Codex 054 反例：两个标签页的新题都是第 2 题，合并后本页那题改成第 3 题；按题号缓存会把 A 的价格（120）显示成 B 的（80）。
    取页面里原样的 barsOf 定义，在合并前后、选择与推进时都与直接 Drill.window 一致。"""
    page = (ROOT / "scripts/bar_drill_page.html").read_text("utf-8")
    decl = next(s for s in page.split("\n") if s.startswith("const barsOf = "))
    out = _node_page(f"""
      const rawA = {{d:[1,2,3],o:[100,100,120],h:[101,101,121],l:[99,99,119],c:[100,100,120],v:[1,1,1]}};
      const rawB = {{...rawA, o:[100,100,80], h:[101,101,81], l:[99,99,79], c:[100,100,80]}};
      const q0 = {{cursor:1, max_cursor:1, actions:[], trades:[], marks:[], pos:null, pending:null}};
      const a = {{...q0, seed:2, id:2, sym:'A', i0:1, left:2, right:1, mirror:false}};
      const b = {{...q0, seed:3, id:2, sym:'B', i0:1, left:2, right:1, mirror:false}};
      const POOL = {{series: {{A: rawA, B: rawB}}}};
      const cache = {{}};
      {decl}
      barsOf(a);                                                 // 本页先缓存了 A（第 2 题）
      const m = DrillSync.merge({{questions: [a]}}, {{questions: [b]}}, 'T');
      const qb = m.questions.find(q => q.seed === 3), qa = m.questions.find(q => q.seed === 2);
      const shown = [barsOf(qb)[2][4], barsOf(qa)[2][4]];
      const st = JSON.parse(JSON.stringify(qb)); st.pending = {{type: 'long'}};
      Drill.step(st, barsOf(qb));                                // 推进也用同一份缓存
      return {{ids: m.questions.map(q => [q.seed, q.id]), shown, truth: [Drill.window(rawB, b)[2][4], Drill.window(rawA, a)[2][4]],
              entry: st.pos.entry, entry_truth: Drill.window(rawB, b)[2][1]}};""")
    assert out["ids"] == [[3, 2], [2, 3]]
    assert out["shown"] == out["truth"] == [80, 120]
    assert out["entry"] == out["entry_truth"] == 80


def _node_page(js):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    code = (f"const Drill = require({json.dumps(str(ENGINE))});\nconst DrillSync = require({json.dumps(str(SYNC))});\n"
            f"const out = (() => {{ {js} }})();\nconsole.log(JSON.stringify(out));")
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_merge_keeps_local_only_conflict_copies_and_is_idempotent():
    out = _node_sync("""
      const q = {seed: 1, id: 1, cursor: 5, max_cursor: 5, actions: [{a: 1}], trades: [], marks: []};
      const copy = {stashed_at: 'T', seed: 1, question: {...q, actions: [{a: 2}]}};
      const srvCopy = {stashed_at: 'S', seed: 1, question: {...q, actions: [{a: 9}]}};
      const m1 = DrillSync.merge({questions: [q], conflict_copies: [copy]}, {questions: [q], conflict_copies: []}, 'T2');
      const m2 = DrillSync.merge(m1, {questions: [q], conflict_copies: []}, 'T3');          // 再读一次（首次写入失败）
      const m3 = DrillSync.merge({questions: [q], conflict_copies: [copy, srvCopy]}, {questions: [q], conflict_copies: [srvCopy]}, 'T4');
      const fork = {...q, cursor: 6, max_cursor: 6, actions: [{a: 1}, {a: 3}]}, ahead = {...q, cursor: 6, max_cursor: 6, actions: [{a: 1}, {a: 4}]};
      const f1 = DrillSync.merge({questions: [fork]}, {questions: [ahead]}, 'A');
      const f2 = DrillSync.merge({questions: [fork], conflict_copies: f1.conflict_copies}, {questions: [ahead]}, 'B');   // 本页没采用合并结果又合一次
      return {m1: [m1.conflict_copies.length, m1.changed], m2: [m2.conflict_copies.length, JSON.stringify(m2.conflict_copies) === JSON.stringify(m1.conflict_copies)],
              m3: m3.conflict_copies.map(c => c.stashed_at), f: [f1.conflict_copies.length, f2.conflict_copies.length]};""")
    assert out["m1"] == [1, True]                       # 本机 1 份、服务端 0 份 → 不减，且要写
    assert out["m2"] == [1, True]                       # 重复合并幂等
    assert out["m3"] == ["S", "T"]                      # 服务端已有的是前缀，本机独有的追加在后
    assert out["f"] == [1, 1]                           # 同一份分叉不会因为重复合并存两次


def test_fork_then_first_post_fails_then_remerge_saves_both_branches(tmp_path, monkeypatch):
    """分叉 → 存档 → 第一次写文件失败 → 重新读取再合并 → 写成：两条动作分支都在（文件那份在题目里，本页那份在冲突副本里）。
    走真实 sv.save（CAS + 只许追加），失败用一次性 OSError 模拟。"""
    store = tmp_path / "bar_drill_v1.json"
    a1, a2, b2 = {"type": "long", "as_of": 300}, {"type": "next", "as_of": 300}, {"type": "short", "as_of": 300}
    base = _q(1, 300, actions=[a1])
    sv.save(store, _rec(0, [base]), META)
    sv.save(store, _rec(1, [{**base, "cursor": 301, "max_cursor": 301, "actions": [a1, a2]}]), META)   # 标签页甲写成第 2 版
    tab = {"questions": [{**base, "cursor": 301, "max_cursor": 301, "actions": [a1, b2]}], "conflict_copies": []}   # 标签页乙
    with pytest.raises(sv.Conflict):
        sv.save(store, _rec(1, tab["questions"]), META)                   # 乙迟到 → 409
    srv = sv.load(store)
    m = _merge(tab, srv)
    assert m["diverged"] == 1
    tab = {"questions": m["questions"], "conflict_copies": m["conflict_copies"]}      # 页面采用合并结果（并写浏览器本地）
    real = sv.write_atomic
    def boom(path, body):
        monkeypatch.setattr(sv, "write_atomic", real)
        raise OSError("磁盘写入失败（模拟）")
    monkeypatch.setattr(sv, "write_atomic", boom)
    with pytest.raises(OSError):
        sv.save(store, _rec(srv["revision"], tab["questions"], conflict_copies=tab["conflict_copies"]), META)
    assert sv.load(store)["revision"] == 2 and not sv.load(store).get("conflict_copies")
    srv = sv.load(store)                                                  # 再读一次、再合并
    m = _merge(tab, srv)
    assert m["changed"] and len(m["conflict_copies"]) == 1
    sv.save(store, _rec(srv["revision"], m["questions"], conflict_copies=m["conflict_copies"]), META)
    d = sv.load(store)
    assert d["questions"][0]["actions"] == [a1, a2]
    assert [c["question"]["actions"] for c in d["conflict_copies"]] == [[a1, b2]]
    m2 = _merge({"questions": d["questions"], "conflict_copies": d["conflict_copies"]}, d)            # 写成后再读：没有新改动
    assert not m2["changed"]


# ---- Codex 054 B2：新题完整结构校验；读入与恢复同校验；旧题宽松分流 ----

META_N = {**META, "series_n": {"GLD": 1000}}


def _nq(seed, cursor=299, *, qid=1, **kw):
    q = {"id": qid, "seed": seed, "sym": "GLD", "i0": 400, "mirror": False, "left": 300, "right": 150,
         "cursor": cursor, "max_cursor": cursor, "pos": None, "pending": None, "trades": [], "marks": [], "actions": [],
         "note": "", "done": False, "created_at": "2026-10-16T00:00:00Z", "version": "bar-drill-v1",
         "engine": {"version": "bar-drill-engine-2", "sha256": "e" * 64}, "data_sha256": "d" * 64}
    q.update(kw)
    return q


GOOD_TRADE = {"side": 1, "entry": 100.0, "entry_i": 300, "exit": 104.0, "exit_i": 320, "reason": "止盈", "exit_kind": "fill",
              "stop0": 98.0, "stop0_late": False, "ret_pct": 4.0, "r": 2.0}


@pytest.mark.parametrize("bad", [
    {"sym": None}, {"sym": "XXX"}, {"i0": "broken"}, {"i0": 100}, {"i0": 900}, {"mirror": "false"}, {"cursor": -9},
    {"cursor": 450, "max_cursor": 450}, {"max_cursor": 10}, {"left": 0}, {"done": "no"}, {"evil": 1}, {"engine": {"version": "x"}},
    {"data_sha256": None}, {"pos": {"side": 2, "entry": 100, "entry_i": 300}}, {"pending": {"type": "buy"}},
    {"trades": [{**GOOD_TRADE, "exit_i": 999}]}, {"trades": [{**GOOD_TRADE, "exit_kind": "magic"}]}, {"actions": [{"as_of": 3}]},
    {"marks": [{"action": "add", "id": "m", "type": "HH", "i": 5000, "price": 1}]},
])
def test_bad_new_question_rejected_and_store_untouched(tmp_path, bad):
    store = tmp_path / "bar_drill_v1.json"
    sv.save(store, _rec(0, [_nq(1)]), META_N)
    before = store.read_bytes()
    with pytest.raises(ValueError):
        sv.save(store, _rec(1, [_nq(1), _nq(2, qid=2, **bad)]), META_N)
    assert store.read_bytes() == before


def test_non_finite_numbers_and_duplicate_ids_rejected(tmp_path):
    store = tmp_path / "bar_drill_v1.json"
    ok = json.dumps(_rec(0, [_nq(1, trades=[GOOD_TRADE])]))
    for raw in (ok.replace('"ret_pct": 4.0', '"ret_pct": NaN'), ok.replace('"ret_pct": 4.0', '"ret_pct": Infinity'),
                ok.replace('"ret_pct": 4.0', '"ret_pct": 1e400')):
        with pytest.raises(ValueError):
            sv.save(store, sv.loads(raw), META_N)
    with pytest.raises(ValueError):
        sv.save(store, _rec(0, [_nq(1), _nq(2)]), META_N)                     # 题号重复
    with pytest.raises(ValueError):
        sv.save(store, _rec(0, [{**_q(1, 3), "seed": 5, "trades": [{"x": float("inf")}]}]), META_N)   # 旧题里的 inf 也拦
    assert not store.exists()
    full = _nq(1, cursor=320, max_cursor=330, trades=[GOOD_TRADE], pending={"type": "long", "stop": 95.0},
               pos={"side": -1, "entry": 101.0, "entry_i": 321, "stop": 105.0, "stop0": 105.0, "target": None, "stop0_late": True, "stop0_set_i": 322},
               actions=[{"type": "long", "as_of": 300, "as_of_bar": 0, "at": "t"}],
               marks=[{"action": "add", "id": "m1", "type": "HH", "i": 310, "bar": 10, "price": 103.5, "as_of": 310, "at": "t"},
                      {"action": "delete", "target": "m1", "as_of": 311, "at": "t"}],
               reopened_from={"seed": 9, "id": 3, "cursor": 320})
    assert sv.save(store, _rec(0, [full], cur=1, settings={"pool": "all"}), META_N)["revision"] == 1


def test_legacy_question_keeps_unknown_fields(tmp_path):
    """没有 engine 的旧版题按 legacy 宽松分流：不认识的字段原样保留，不删、不因缺新字段被拒。"""
    store = tmp_path / "bar_drill_v1.json"
    old = {**_q(7, 12), "some_old_field": {"k": [1, 2]}}
    sv.save(store, _rec(0, [old, _nq(8, qid=2)]), META_N)
    assert sv.load(store, META_N)["questions"][0]["some_old_field"] == {"k": [1, 2]}


def test_load_and_recover_validate_without_quarantine(tmp_path):
    store = tmp_path / "bar_drill_v1.json"
    sv.save(store, _rec(0, [_nq(1)]), META_N)
    d = json.loads(store.read_text("utf-8"))
    d["questions"][0]["mirror"] = "false"
    store.write_text(json.dumps(d), "utf-8")
    before = store.read_bytes()
    with pytest.raises(sv.InvalidStore):
        sv.load(store, META_N)
    with pytest.raises(sv.InvalidStore):
        sv.save(store, _rec(1, [_nq(1)]), META_N)
    assert store.read_bytes() == before and not list(tmp_path.glob("*.corrupt-*"))     # 不改名、不隔离
    assert sv.check_store(store, META_N) == 1
    bad_backup = tmp_path / "backup.json"
    bad_backup.write_bytes(before)
    store.unlink()
    with pytest.raises(ValueError):
        sv.recover_from(store, bad_backup, META_N)
    assert not store.exists()


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def test_page_legacy_unfinished_question_is_read_only_and_reopens(tmp_path):
    """执行路径（无头 Chrome，file:// 只存浏览器）：旧引擎未完题点推进/下单/结束都不动它；另开的新题带当前引擎身份、
    同一段 K 线、从已看到的位置空仓开始，且能用当前引擎推进。"""
    if not CHROME.exists():
        pytest.skip("没有 Chrome")
    n = 20
    c = [100 + i for i in range(n)]
    pool = {"meta": {"etf": ["GLD"], "stock": [], "sha256": {"GLD": "d" * 64}},
            "series": {"GLD": {"d": [20000103 + i for i in range(n)], "o": c, "h": [x + 1 for x in c], "l": [x - 1 for x in c], "c": c, "v": [1] * n}}}
    html = bd.render(pool)
    legacy = {"id": 1, "seed": 5, "sym": "GLD", "i0": 9, "mirror": False, "left": 5, "right": 5, "cursor": 6, "max_cursor": 7,
              "pos": {"side": 1, "entry": 105, "entry_i": 5, "stop": None, "stop0": None, "target": None}, "pending": None,
              "trades": [], "marks": [], "actions": [], "note": "", "done": False}
    seed = f'<script>localStorage.setItem("bar_drill_v1", {json.dumps(json.dumps({"questions": [legacy], "cur": 1}))});</script>'
    probe = """<script>
      const out = {}; const q = Q(); const snap = JSON.stringify(q);
      out.banner = !$("legacy").hidden; out.next_disabled = $("next").disabled;
      advance(1); order("short"); endQ(); $("cancel").onclick();
      out.untouched = JSON.stringify(DB.questions.find(x => x.seed === 5)) === snap;
      $("reopen").onclick();
      const nq = Q(); out.new = {id: nq.id, cur_engine: curEngine(nq), data: nq.data_sha256, sym: nq.sym, i0: nq.i0, cursor: nq.cursor,
                                pos: nq.pos, from: nq.reopened_from, banner: !$("legacy").hidden};
      advance(1); out.new_cursor_after = Q().cursor; out.n = DB.questions.length;
      document.body.setAttribute("data-probe", JSON.stringify(out));
    </script>"""
    assert html.count("<script>") == 1 and html.count("</body></html>") == 1
    html = html.replace("<script>", seed + "<script>", 1).replace("</body></html>", probe + "</body></html>")
    f = tmp_path / "page.html"
    f.write_text(html, "utf-8")
    r = subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'prof'}", "--dump-dom", f.as_uri()],
                       capture_output=True, text=True, timeout=90)
    import html as _h
    import re
    m = re.search(r'data-probe="([^"]*)"', r.stdout)
    assert m, r.stdout[-500:] + r.stderr[-500:]
    out = json.loads(_h.unescape(m.group(1)))
    assert out["banner"] and out["next_disabled"] and out["untouched"]
    nw = out["new"]
    assert nw["id"] == 2 and nw["cur_engine"] and nw["data"] == "d" * 64 and (nw["sym"], nw["i0"]) == ("GLD", 9)
    assert nw["cursor"] == 7 and nw["pos"] is None and nw["from"]["seed"] == 5 and not nw["banner"]
    assert out["new_cursor_after"] == 8 and out["n"] == 2
