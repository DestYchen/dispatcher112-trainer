import pytest

from app.config import settings
from app.generation.llm import validate_local_llm_url
from app.transport_security import (
    http_verify,
    queue_settings,
    redis_tls,
    verified_context,
    websocket_tls,
)


def test_redis_tls_always_validates_certificate_and_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "redis_url", "rediss://redis:6379/3")
    monkeypatch.setattr(settings, "tls_ca_file", "/run/tls/ca.crt")
    options = redis_tls()
    assert options == {
        "ssl_ca_certs": "/run/tls/ca.crt",
        "ssl_cert_reqs": "required",
        "ssl_check_hostname": True,
    }
    queue = queue_settings()
    assert queue.ssl and queue.ssl_check_hostname and queue.ssl_cert_reqs == "required"
    assert queue.database == 3 and queue.ssl_ca_certs == "/run/tls/ca.crt"


def test_missing_ca_fails_closed_without_disabling_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_context.cache_clear()
    monkeypatch.setattr(settings, "tls_ca_file", "/missing/project-ca.crt")
    with pytest.raises(FileNotFoundError):
        http_verify()
    with pytest.raises(FileNotFoundError):
        websocket_tls("https://telephony:8089/ari")


def test_explicit_local_development_remains_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tls_ca_file", None)
    monkeypatch.setattr(settings, "redis_url", "redis://redis:6379/0")
    assert http_verify() is True
    assert redis_tls() == {} and websocket_tls("http://telephony:8088/ari") == {}
    assert not queue_settings().ssl


def test_https_local_model_is_allowed_but_external_host_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "local_llm_url", "https://ollama:11435/")
    assert validate_local_llm_url() == "https://ollama:11435"
    monkeypatch.setattr(settings, "local_llm_url", "https://example.com/")
    with pytest.raises(ValueError, match="Внешний"):
        validate_local_llm_url()
