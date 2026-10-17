"""核验一次运行的状态文件（schema 2 + run_id/session/command 全部相符）后打印 `<overall> <counts.new>`。

daily_update.sh 的主墙覆盖核查用（Codex 059 A2）：返回码 1 既可能是「有缺口」也可能是 Python 崩溃，
只有身份相符的状态才允许解读。缺失/损坏/不符 → 打印 `status_missing` / `status_corrupt` / `status_mismatch`（无计数）。
"""
import json
import sys


def main(argv: list) -> int:
    p, rid, sess, cmd = argv[1:5]
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        print("status_missing"); return 0
    except Exception:
        print("status_corrupt"); return 0
    if not (isinstance(d, dict) and d.get("schema") == 2 and d.get("run_id") == rid
            and d.get("session") == sess and d.get("command") == cmd):
        print("status_mismatch"); return 0
    ov = d.get("overall") or "status_corrupt"
    n = (d.get("counts") or {}).get("new")
    print(f"{ov} {n}" if type(n) is int and n >= 0 else ov)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
