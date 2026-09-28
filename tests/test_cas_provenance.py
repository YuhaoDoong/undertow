"""017 A01/A02：分块内容寻址存储可逐字节还原任意版本；消费即留痕；损坏可见。"""
import gzip
import hashlib
import json

import pytest

from undertow.collect import cas, provenance


def _series(n, first="1.0"):
    rows = [f"2026-01-{i % 28 + 1:02d},{first if i == 0 else i * 1.5}" for i in range(n)]
    return ("DATE,VALUE\n" + "\n".join(rows) + "\n").encode()


def test_early_row_revision_is_recoverable_and_deduplicated(tmp_path):
    v1 = _series(20000)
    v2 = _series(20000, first="999")                        # 017 probe：只改最早一行
    v3 = v2 + b"2026-09-28,7.0\n"                           # 再追加一行
    r1 = cas.put(v1, root=tmp_path)
    r2 = cas.put(v2, root=tmp_path)
    r3 = cas.put(v3, root=tmp_path)
    for v, r in ((v1, r1), (v2, r2), (v3, r3)):
        assert cas.get(r["sha256"], root=tmp_path) == v      # 逐字节还原
    assert r1["n_chunks"] > 5
    assert r2["new_chunks"] <= 2 and r3["new_chunks"] <= 2   # 改一行 / 追加一行只新增附近的块
    assert cas.put(v3, root=tmp_path)["new_chunks"] == 0     # 重复存入不增加


def test_corrupt_chunk_is_detected_not_returned(tmp_path):
    r = cas.put(_series(5000), root=tmp_path)
    rec = json.loads((tmp_path / "recipes" / r["sha256"][:2] / f"{r['sha256']}.json").read_text())
    obj = tmp_path / "objects" / rec["chunks"][1][:2] / f"{rec['chunks'][1]}.gz"
    obj.write_bytes(gzip.compress(b"tampered", mtime=0))
    with pytest.raises(cas.CasCorrupt):
        cas.get(r["sha256"], root=tmp_path)
    v = cas.verify(root=tmp_path)
    assert v["recipes"] == 1 and len(v["bad"]) == 1


def test_chunking_is_lossless_on_arbitrary_bytes():
    raw = bytes(range(256)) * 700 + b",\n" * 3000
    assert b"".join(cas.chunks(raw)) == raw


def test_provenance_records_consumed_version_even_if_cache_changes_later(tmp_path, monkeypatch):
    from undertow.collect.cache import FileCache
    monkeypatch.setattr(cas, "ROOT", tmp_path / "cas")
    fc = FileCache(root=tmp_path / "cache")
    provenance.begin("report", ["report", "gold"])
    fc.set("fred_DGS10", "DATE,V\n2026-09-25,4.1\n")
    assert fc.get("fred_DGS10", None) is not None
    fc.set("acct_secret", {"x": 1})                         # 非公开前缀不登记
    consumed = [it for it in provenance._run["items"]]
    man = provenance.finish(0, out_dir=tmp_path / "man")
    fc.set("fred_DGS10", "DATE,V\n2026-09-25,9.9\n")        # 研报之后缓存被改写
    run = json.loads(man.read_text())
    assert {it["key"] for it in run["items"]} == {"fred_DGS10"} and len(consumed) == 1
    got = provenance.restore_inputs(man)
    assert got["complete"] and len(got["items"]) == 1
    raw = got["items"][0]["raw"]
    assert b"4.1" in raw and b"9.9" not in raw
    assert run["items"][0]["status"] == "fresh_fetch" and run["rc"] == 0


def test_provenance_inactive_outside_cli(tmp_path):
    from undertow.collect.cache import FileCache
    assert not provenance.active()
    FileCache(root=tmp_path).set("fred_X", "a")               # 不开启时不写任何东西
    assert provenance.finish(0, out_dir=tmp_path / "m") is None


def test_stale_cache_is_labelled(tmp_path, monkeypatch):
    import os
    import time
    from undertow.collect.cache import FileCache
    monkeypatch.setattr(cas, "ROOT", tmp_path / "cas")
    fc = FileCache(root=tmp_path / "c")
    fc.set("yahoo_GC", {"a": 1})
    p = tmp_path / "c" / "yahoo_GC.json"
    d = json.loads(p.read_text()); d["fetched_at"] = time.time() - 7200; p.write_text(json.dumps(d))
    provenance.begin("report", [])
    fc.get("yahoo_GC", None)                                  # 永不过期的读取，但记录年龄
    it = provenance._run["items"][0]
    assert it["status"] == "cache_hit" and it["age_s"] >= 7000
    provenance._run["items"].clear(); provenance._run["_seen"].clear()
    os.utime(p, None)
    provenance.consume_cache_raw("yahoo_GC", p.read_bytes(), status="cache_hit", ttl_s=60)
    assert provenance._run["items"][0]["status"] == "stale_cache"
    provenance.finish(0, out_dir=tmp_path / "m")


def test_monthly_corrupt_is_quarantined(tmp_path):
    from undertow.collect import input_archive as ia
    c = tmp_path / "cache"; c.mkdir()
    (c / "fred_A.json").write_text(json.dumps({"fetched_at": 1, "data": "DATE,V\n1,2\n"}))
    out = tmp_path / "out"
    m = out / "monthly" / "2026-09"; m.mkdir(parents=True)
    (m / "fred_A.json.gz").write_bytes(b"not gzip")
    r = ia.archive("2026-09-28", cache_dir=c, out_dir=out)
    assert any("隔离" in i for i in r["issues"]) and list(m.glob("fred_A.json.gz.corrupt-*"))
    assert (m / "fred_A.json.gz").exists() and r["stats"]["cas_new_recipes"] == 1
    assert (out / "cas" / "recipes").exists()


# —— Codex 019-01 ——
def test_put_with_corrupt_chunk_repairs_and_keeps_evidence(tmp_path):
    raw = _series(3000)
    r = cas.put(raw, root=tmp_path)
    obj = next((tmp_path / "objects").rglob("*.gz"))
    obj.write_bytes(b"broken")
    r2 = cas.put(raw, root=tmp_path)                                   # 以前：new_chunks=0 快速成功，随后 get 失败
    assert r2["repairs"] and cas.get(r["sha256"], root=tmp_path) == raw
    assert any(p.read_bytes() == b"broken" for p in (tmp_path / "objects").rglob("*.corrupt-*"))


def test_put_with_corrupt_recipe_quarantines_old_bytes(tmp_path):
    raw = _series(3000)
    cas.put(raw, root=tmp_path)
    rp = next((tmp_path / "recipes").rglob("*.json"))
    rp.write_text("BROKEN-RECIPE")
    r2 = cas.put(raw, root=tmp_path)
    assert r2["repairs"] and cas.get(r2["sha256"], root=tmp_path) == raw
    assert any(p.read_bytes() == b"BROKEN-RECIPE" for p in (tmp_path / "recipes").rglob("*.corrupt-*"))


def test_verify_reports_structurally_bad_recipe_and_continues(tmp_path):
    good = cas.put(_series(2000), root=tmp_path)
    bad = cas.put(_series(2100), root=tmp_path)
    rp = tmp_path / "recipes" / bad["sha256"][:2] / f"{bad['sha256']}.json"
    rp.write_text(json.dumps({"schema": 1, "sha256": bad["sha256"]}))       # 合法 JSON、缺字段
    v = cas.verify(root=tmp_path)
    assert v["recipes"] == 2 and v["ok"] == 1 and v["bad"][0][0] == bad["sha256"]
    assert cas.get(good["sha256"], root=tmp_path)


# —— Codex 019-02 ——
def test_cache_race_between_read_and_record_uses_same_bytes(tmp_path, monkeypatch):
    from undertow.collect.cache import FileCache
    monkeypatch.setattr(cas, "ROOT", tmp_path / "cas")
    fc = FileCache(root=tmp_path / "c")
    fc.set("fred_demo", {"version": "A"})
    orig = provenance.consume_cache_raw
    path = tmp_path / "c" / "fred_demo.json"

    def racing(key, raw, **kw):                                  # 读取之后、登记之前文件被换成 B
        path.write_text(json.dumps({"fetched_at": 1, "data": {"version": "B"}}))
        return orig(key, raw, **kw)
    monkeypatch.setattr(provenance, "consume_cache_raw", racing)
    provenance.begin("report", [])
    returned = fc.get("fred_demo", None)
    man = provenance.finish(0, out_dir=tmp_path / "m")
    got = provenance.restore_inputs(man)
    assert returned == {"version": "A"}
    assert json.loads(got["items"][0]["raw"])["data"] == {"version": "A"}      # 留痕的也是 A


def test_same_key_two_versions_restore_separately(tmp_path, monkeypatch):
    from undertow.collect.cache import FileCache
    monkeypatch.setattr(cas, "ROOT", tmp_path / "cas")
    fc = FileCache(root=tmp_path / "c")
    provenance.begin("report", [])
    fc.set("fred_X", "v1"); fc.get("fred_X", None)
    fc.set("fred_X", "v2"); fc.get("fred_X", None)
    man = provenance.finish(0, out_dir=tmp_path / "m")
    got = provenance.restore_inputs(man)
    datas = [json.loads(i["raw"])["data"] for i in got["items"]]
    assert datas == ["v1", "v2"] and [i["seq"] for i in got["items"]] == [0, 1]


def test_snapshot_identity_from_single_read_and_overwrite_detected(tmp_path, monkeypatch):
    from datetime import date
    from undertow.collect.store import SnapshotStore
    st = SnapshotStore(root=tmp_path / "snap")
    st.save("options", "GLD", {"a": 1}, on_date=date(2026, 9, 28))
    provenance.begin("report", [])
    payload, ident = st.load_with_identity("options", "GLD", date(2026, 9, 28))
    p = st.path_of("options", "GLD", date(2026, 9, 28))
    assert payload == {"a": 1} and ident["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert ident["captured_at"] is not None
    man = provenance.finish(0, out_dir=tmp_path / "m")
    st.save("options", "GLD", {"a": 2}, on_date=date(2026, 9, 28))          # 同日覆盖
    got = provenance.restore_inputs(man)
    assert not got["complete"] and "已被覆盖" in got["problems"][0]


def test_offline_replay_reproduces_analysis(tmp_path, monkeypatch):
    """断网回放：按清单还原长桥日线原文 → 解析 → 与在线时的计分结果完全一致。"""
    import subprocess
    from datetime import date
    from undertow.analyze import skew_reading as skr
    from undertow.collect import longbridge_kline as lk
    monkeypatch.setattr(cas, "ROOT", tmp_path / "cas")
    out = json.dumps([{"time": "2026-09-28T13:30:00Z", "open": "100", "high": "102", "low": "99", "close": "101", "volume": "1"},
                      {"time": "2026-09-29T13:30:00Z", "open": "101", "high": "104", "low": "100", "close": "103", "volume": "1"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out + "\n统计行", stderr=""))
    provenance.begin("dirledger", [])
    online = lk.fetch_bars("GLD.US", period="day", count=400)
    man = provenance.finish(0, out_dir=tmp_path / "m")

    def no_net(*a, **k):
        raise OSError("断网")
    monkeypatch.setattr(subprocess, "run", no_net)
    got = provenance.restore_inputs(man)
    assert got["complete"]
    offline = lk.bars_from_rows(lk.parse_rows(got["items"][0]["raw"], "GLD.US"))
    assert offline == online
    f = lambda bars: skr.forward_returns([(b["ts"].date(), b["open"], b["close"]) for b in bars], date(2026, 9, 28),
                                         (1, 2), closed_through=date(2026, 9, 29))
    assert f(offline) == f(online) and f(offline)["ret_2d"] == pytest.approx(0.03)
