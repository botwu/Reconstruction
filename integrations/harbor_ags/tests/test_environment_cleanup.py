"""AGS 已停止沙箱的幂等清理，以及查询失败时保留未确认状态。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from e2b.exceptions import AuthenticationException, NotFoundException, SandboxException

from harbor_ags.environment import AGSPrebuiltEnvironment
from harbor_ags.sandbox_ledger import SandboxCleanupError


def _environment() -> tuple[AGSPrebuiltEnvironment, SimpleNamespace]:
    sandbox = SimpleNamespace(sandbox_id="owned-sandbox", kill=Mock(), get_info=Mock())
    environment = object.__new__(AGSPrebuiltEnvironment)
    environment._sandbox = sandbox
    environment._created_ids = {sandbox.sandbox_id}
    environment._terminal_ids = set()
    environment._lifecycle_lock = asyncio.Lock()
    environment._request_timeout_sec = 1
    environment._record = AsyncMock()
    return environment, sandbox


@pytest.mark.parametrize("killed", [True, False])
def test_stop_accepts_confirmed_delete(killed: bool) -> None:
    environment, sandbox = _environment()
    sandbox.kill.return_value = killed

    asyncio.run(environment.stop(delete=True))

    environment.assert_cleanup_verified()
    assert environment._sandbox is None
    sandbox.get_info.assert_not_called()


def test_stop_accepts_absent_sandbox_after_rejected_delete() -> None:
    environment, sandbox = _environment()
    # R04:2141 实跑：AGS DELETE 对 STOPPED 返回 400，GET 同一 ID 返回 404。
    sandbox.kill.side_effect = SandboxException("400: Instance is in STOPPED state")
    sandbox.get_info.side_effect = NotFoundException("Sandbox owned-sandbox not found")

    asyncio.run(environment.stop(delete=True))

    environment.assert_cleanup_verified()
    assert environment._sandbox is None


@pytest.mark.parametrize(
    "query_error",
    [None, AuthenticationException("查询认证失败"), TimeoutError("查询超时")],
)
def test_stop_preserves_failure_without_absence_confirmation(query_error: Exception | None) -> None:
    environment, sandbox = _environment()
    sandbox.kill.side_effect = SandboxException("400: Instance is in STOPPED state")
    if query_error is not None:
        sandbox.get_info.side_effect = query_error
    else:
        sandbox.get_info.return_value = SimpleNamespace(sandbox_id=sandbox.sandbox_id)

    with pytest.raises(SandboxCleanupError):
        asyncio.run(environment.stop(delete=True))

    with pytest.raises(SandboxCleanupError):
        environment.assert_cleanup_verified()
    assert environment._sandbox is sandbox


def test_stop_does_not_confirm_cleanup_when_lookup_is_cancelled() -> None:
    environment, sandbox = _environment()
    sandbox.kill.side_effect = SandboxException("销毁失败")
    sandbox.get_info.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(environment.stop(delete=True))

    with pytest.raises(SandboxCleanupError):
        environment.assert_cleanup_verified()
    assert environment._sandbox is sandbox
