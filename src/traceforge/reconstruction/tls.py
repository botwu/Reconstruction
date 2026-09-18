"""给 httpx / Anthropic / E2B 钉一份能过公司网关的 CA。

本机默认 ``SSL_CERT_FILE`` 经常只有 mkcert。httpx 0.28 会只用这一份，
TokenHub 和 ``api.ap-beijing.tencentags.com`` 就会 SSL 失败。
系统 CA（必要时拼上 mkcert）对这两处都能完成握手。
"""

from __future__ import annotations

import os
from pathlib import Path

SYSTEM_CA_CANDIDATES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"),
    Path("/etc/ssl/cert.pem"),
)
_TLS_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _first_existing(paths: tuple[Path, ...]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def resolve_tls_bundle() -> str | None:
    override = os.environ.get("TRACEFORGE_SSL_CERT_FILE", "").strip()
    if override and Path(override).is_file():
        return override
    system = _first_existing(SYSTEM_CA_CANDIDATES)
    extra = os.environ.get("SSL_CERT_FILE", "").strip()
    extra_path = Path(extra) if extra else None
    if system is None:
        return extra if extra_path is not None and extra_path.is_file() else None
    if extra_path is None or not extra_path.is_file() or extra_path.resolve() == system.resolve():
        return str(system)
    dest = Path(os.environ.get("TRACEFORGE_TLS_BUNDLE", "/tmp/traceforge-ca-bundle.pem"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = [system.read_bytes().rstrip() + b"\n", extra_path.read_bytes().rstrip() + b"\n"]
    payload = b"".join(parts)
    if not dest.is_file() or dest.read_bytes() != payload:
        dest.write_bytes(payload)
    return str(dest)


def pin_process_tls(env: dict[str, str] | None = None) -> str | None:
    """把进程或给定 env 的 CA 环境变量钉到可用 bundle。"""

    target = env if env is not None else os.environ
    previous = target.get("SSL_CERT_FILE")
    if previous and env is None:
        os.environ.setdefault("TRACEFORGE_PREVIOUS_SSL_CERT_FILE", previous)
    bundle = resolve_tls_bundle()
    if not bundle:
        return None
    for name in _TLS_ENV:
        target[name] = bundle
    return bundle
