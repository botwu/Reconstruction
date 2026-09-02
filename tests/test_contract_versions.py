"""M1 当前发布版本的显式冻结门。"""

from traceforge import __version__
from traceforge.trajectory.contracts import (
    COMPILER_CONTRACT_VERSION,
    EVENT_SCHEMA,
    TOOL_PAIRING_SCHEMA,
)


def test_m1_v3_release_versions_are_explicitly_frozen() -> None:
    assert __version__ == "0.3.0"
    assert COMPILER_CONTRACT_VERSION == "trajectory-compiler-m1ab-v4"
    assert TOOL_PAIRING_SCHEMA == "traceforge.tool-pairing.v3"
    # 合法 Event 输出形态未变化，不能为了同步编号而虚假升级。
    assert EVENT_SCHEMA == "traceforge.event-occurrence.v3"
