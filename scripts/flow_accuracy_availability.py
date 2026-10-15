"""flow-accuracy-explore-v1 逐行可用时刻与输入绑定（Codex 049 D / 050 C）—— 只读注记：不重取行情、不重算收益、不改原 run 产物。

用法：python3 scripts/flow_accuracy_availability.py RUN COMMIT
- RUN：data/backtest/flow-accuracy-explore-v1/<RUN>/result.json（读其 prospective_rows）。
- COMMIT：绑定输入所用的 git 提交（一般就是 run 产物的提交）。信号文件与期权快照都从【该提交的树】读（git show），不读工作区，
  第三人用同一提交即可复现本注记。
输出 data/backtest/flow-accuracy-explore-v1/<RUN>-availability/{availability.json, STATUS.md}；目录已存在 → 拒绝覆盖。

可用时刻判据（每行）：信号行由快照对 (prev_date, D) 算出 → 两份快照 options/<SYM>/<日期>.json.gz 在该提交中的版本，
其 captured_at 都早于执行日 T 的 09:30 美东 → preopen；任一晚于 → after_open（提交里的版本是开盘后才抓/重抓的，
开盘前是否已有同样内容未知）；文件、字段或 prev_date 缺 → unknown。不回填、不猜盘前时间。
⚠️ preopen 只说明【输入数据】在开盘前已落盘；信号行本身何时算出没有存档（signals 文件无 captured_at/recorded_at），
回放段（D < 2026-09-28）本来就是用之后写成的算法事后计算的（样本内）。call_code_sha 逐行照抄，缺则 None。
⚠️ 日线：run1/run2 未封存所用的长桥日线，这里不重取；收益字段原样引用 run 产物，日线输入绑定 = unknown。

统计说明（就地，Codex 049）：命中率的二项双尾 p 与 Wilson 下界假设各日独立同分布；这是同一批数据上的多组探索
（8 品种 × 3 段 × 多口径），名义 p 不做多重校正，不能用来挑规则；正式检验仍按冻结族与 12/31 首次正式运行。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data/backtest/flow-accuracy-explore-v1"
VERSION = "flow-accuracy-availability-v1-20261015"
PROSPECTIVE_START = "2026-09-28"
_ET = ZoneInfo("America/New_York")


def _git_blob(commit: str, path: str) -> tuple[bytes | None, str | None]:
    """→ (内容, blob id)；该提交里没有这个文件 → (None, None)。"""
    r = subprocess.run(["git", "rev-parse", f"{commit}:{path}"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None, None
    oid = r.stdout.strip()
    b = subprocess.run(["git", "cat-file", "blob", oid], cwd=ROOT, capture_output=True)
    return (b.stdout, oid) if b.returncode == 0 else (None, None)


def captured_at(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        v = json.loads(gzip.decompress(raw)).get("captured_at")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()


def open_et(T: str) -> datetime:
    y, m, d = map(int, T.split("-"))
    return datetime.combine(datetime(y, m, d).date(), time(9, 30), tzinfo=_ET)


def classify(caps: list[str | None], T: str) -> str:
    """全部快照 captured_at 早于 T 开盘 → preopen；任一晚于 → after_open；任一缺 → unknown（缺优先于晚）。"""
    if not caps or any(c is None for c in caps):
        return "unknown"
    return "preopen" if all(datetime.fromisoformat(c) < open_et(T) for c in caps) else "after_open"


def annotate(result: dict, commit: str) -> dict:
    out = {"version": VERSION, "commit": commit, "bars_binding": "unknown（run 未封存日线；收益字段原样引用 run 产物）",
           "signals": {}, "rows": [], "counts": {}}
    for r in result["results"]:
        inst, sym = r["inst"], r["sym"]
        raw, oid = _git_blob(commit, f"data/history/signals/{inst}.json")
        out["signals"][inst] = oid
        sig = {x["date"]: x for x in json.loads(raw)} if raw else {}
        c = {"preopen": 0, "after_open": 0, "unknown": 0}
        hits = {"preopen": [0, 0]}
        for row in r["prospective_rows"]:
            s = sig.get(row["D"]) or {}
            prev = s.get("prev_date")
            snaps = []
            for d in ([prev] if prev else []) + [row["D"]]:
                b, soid = _git_blob(commit, f"data/snapshots/options/{sym}/{d}.json.gz")
                snaps.append({"date": d, "blob": soid, "captured_at": captured_at(b)})
            av = classify([x["captured_at"] for x in snaps] if prev else [], row["T"])
            c[av] += 1
            sg = 1 if row["dir"] in ("偏多", "看涨") else (-1 if row["dir"] in ("偏空", "看跌") else 0)
            if av == "preopen" and sg and row.get("r_oc") not in (None, 0):
                hits["preopen"][1] += 1
                hits["preopen"][0] += (row["r_oc"] > 0) == (sg > 0)
            out["rows"].append({"inst": inst, "D": row["D"], "T": row["T"], "prev_date": prev, "snapshots": snaps,
                                "open_et": open_et(row["T"]).isoformat(), "availability": av,
                                "call_code_sha": s.get("call_code_sha"), "dir": row["dir"]})
        out["counts"][inst] = {**c, "open_to_close_preopen_only": {"hits": hits["preopen"][0], "n": hits["preopen"][1]}}
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    run, commit = argv[1], argv[2]
    out_dir = BASE / f"{run}-availability"
    if out_dir.exists():
        print(f"{out_dir} 已存在：拒绝覆盖", file=sys.stderr)
        return 2
    raw, run_oid = _git_blob(commit, f"data/backtest/flow-accuracy-explore-v1/{run}/result.json")
    if raw is None:
        print(f"提交 {commit} 里没有 {run}/result.json", file=sys.stderr)
        return 2
    payload = annotate(json.loads(raw), commit)
    payload.update(run=run, run_result_blob=run_oid,
                   script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    out_dir.mkdir(parents=True)
    (out_dir / "availability.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# {VERSION} · {run}（注记，非重算）", "",
             f"输入绑定：提交 `{commit}` 的树（信号文件、期权快照、{run}/result.json）。日线 = unknown（未封存）。", "",
             "| 品种 | preopen | after_open | unknown | 仅 preopen 行的开→收命中 |", "|---|---|---|---|---|"]
    for inst, c in payload["counts"].items():
        h = c["open_to_close_preopen_only"]
        lines.append(f"| {inst} | {c['preopen']} | {c['after_open']} | {c['unknown']} | {h['hits']}/{h['n']} |")
    lines += ["", "preopen = 两份输入快照（该提交中的版本）captured_at 都早于执行日 09:30 美东；只证明输入在开盘前已落盘，"
              "不证明信号行当时已算出。after_open / unknown 不得当作可执行样本。",
              "二项 p / Wilson 假设各日独立同分布；本表属同一批数据上的多组探索，名义值不做多重校正、不得用来挑规则。"]
    (out_dir / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
