"""复用原生 Harbor 生命周期，仅补足网关请求的运行契约。"""

import hashlib
import json
import shlex
from importlib.resources import as_file, files
from pathlib import Path

from harbor_ags.agent import LosslessHermesAgent


class GatewayHermesAgent(LosslessHermesAgent):
    """原生捕获、对账和清理不变；独立入口约束流式及模型字面值。"""

    async def setup(self, environment):
        lock = json.loads(Path(__file__).with_name("ripgrep_lock.json").read_text())
        archive = Path(__file__).with_name("vendor") / lock["archive"]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["archive_sha256"]:
            raise ValueError("ripgrep 运行依赖压缩包哈希不匹配")
        await super().setup(environment)
        resource = files("harbor_ags.resources").joinpath("hermes_harness.py")
        with as_file(resource) as source:
            await environment.upload_file(source, "/tmp/harbor_ags_runtime/hermes_harness_base.py")
        await environment.upload_file(
            Path(__file__).with_name("gateway_harness.py"),
            "/tmp/harbor_ags_runtime/hermes_harness.py",
        )
        remote = "/tmp/harbor_ags_runtime"
        target = f"{remote}/vendor/{archive.name}"
        await environment.upload_file(archive, target)
        result = await self.exec_as_root(
            environment,
            command=(
                'test "$(uname -s)" = Linux && test "$(uname -m)" = x86_64 && '
                f'test "$(sha256sum {shlex.quote(target)} | cut -c1-64)" '
                f'= {shlex.quote(lock["archive_sha256"])} && '
                f"mkdir -p {remote}/bin && tar -xzf {shlex.quote(target)} "
                f"-C {remote}/bin --strip-components=1 {shlex.quote(lock['member'])} && "
                f"install -m 755 {remote}/bin/rg /usr/local/bin/rg && "
                f'test "$(sha256sum /usr/local/bin/rg | cut -c1-64)" '
                f'= {shlex.quote(lock["binary_sha256"])} && /usr/local/bin/rg --version'
            ),
            timeout_sec=30,
        )
        if (result.return_code != 0
                or result.stdout.split()[:2] != ["ripgrep", lock["version"]]):
            raise ValueError(
                f"ripgrep 运行依赖校验失败: exit={result.return_code}, "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )
