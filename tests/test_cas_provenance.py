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
    assert b"4.1" in got["cache:fred_DGS10"] and b"9.9" not in got["cache:fred_DGS10"]
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
    provenance.consume_cache_file("yahoo_GC", p, status="cache_hit", ttl_s=60)
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
