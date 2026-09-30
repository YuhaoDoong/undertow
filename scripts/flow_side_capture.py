"""资金流代理一致性研究的采集（兼容入口）：实际逻辑在 `python3 -m undertow.cli shadow flowside`（session ⑬ 用 run_bound 调它）。

  python3 scripts/flow_side_capture.py [gold silver]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    from undertow.cli import main
    sys.exit(main(["shadow", "flowside", *sys.argv[1:]]))
