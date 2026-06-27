"""包入口：支持 `python -m undertow <command>`（等价于 `python -m undertow.cli`）。"""
from undertow.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
