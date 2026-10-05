import subprocess

import pytest

from core.http import build_ssl_context


def test_missing_extra_ca_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError, match="fetch_issuer_cert"):
        build_ssl_context([tmp_path / "nope.pem"])


def test_extra_ca_is_added_and_verification_stays_on(tmp_path):
    pem = tmp_path / "ca.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=Test Extra CA", "-keyout", str(tmp_path / "k.pem"),
                    "-out", str(pem)], check=True, capture_output=True)
    base = build_ssl_context().cert_store_stats()["x509_ca"]
    ctx = build_ssl_context([pem])
    assert ctx.cert_store_stats()["x509_ca"] == base + 1
    import ssl
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_x509_strict_relaxed_only_when_asked():
    import ssl
    strict = build_ssl_context()
    relaxed = build_ssl_context(x509_strict=False)
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        assert not relaxed.verify_flags & ssl.VERIFY_X509_STRICT
    # 其他驗證不受影響
    assert relaxed.verify_mode == ssl.CERT_REQUIRED and relaxed.check_hostname
    assert strict.verify_mode == ssl.CERT_REQUIRED
