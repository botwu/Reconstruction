"""支持通过 ``python -m traceforge`` 启动命令行。"""

from __future__ import annotations

import sys

if sys.version_info < (3, 12):
    raise SystemExit(
        "TraceForge 需要 Python 3.12 或更高版本；请使用 integrations/harbor_ags/.venv/bin/python。"
    )

from traceforge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
