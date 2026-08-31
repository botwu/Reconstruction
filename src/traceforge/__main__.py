"""支持通过 ``python -m traceforge`` 启动命令行。"""

from traceforge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
