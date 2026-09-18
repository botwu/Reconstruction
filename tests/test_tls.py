from __future__ import annotations

from pathlib import Path

from traceforge.reconstruction.tls import pin_process_tls, resolve_tls_bundle


def test_resolve_prefers_system_ca_over_mkcert_only(tmp_path: Path, monkeypatch) -> None:
    system = tmp_path / "system.pem"
    extra = tmp_path / "mkcert.pem"
    system.write_text("SYSTEM\n", encoding="utf-8")
    extra.write_text("MKCERT\n", encoding="utf-8")
    monkeypatch.setattr(
        "traceforge.reconstruction.tls.SYSTEM_CA_CANDIDATES",
        (system,),
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(extra))
    monkeypatch.delenv("TRACEFORGE_SSL_CERT_FILE", raising=False)
    dest = tmp_path / "combined.pem"
    monkeypatch.setenv("TRACEFORGE_TLS_BUNDLE", str(dest))
    bundle = resolve_tls_bundle()
    assert bundle == str(dest)
    text = dest.read_text(encoding="utf-8")
    assert "SYSTEM" in text and "MKCERT" in text


def test_pin_process_tls_sets_httpx_env(tmp_path: Path, monkeypatch) -> None:
    system = tmp_path / "ca.pem"
    system.write_text("CA\n", encoding="utf-8")
    monkeypatch.setattr(
        "traceforge.reconstruction.tls.SYSTEM_CA_CANDIDATES",
        (system,),
    )
    monkeypatch.delenv("TRACEFORGE_SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    env: dict[str, str] = {}
    assert pin_process_tls(env) == str(system)
    assert env["SSL_CERT_FILE"] == str(system)
    assert env["REQUESTS_CA_BUNDLE"] == str(system)
