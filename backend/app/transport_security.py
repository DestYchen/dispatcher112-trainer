import ssl
from functools import lru_cache
from typing import Any

from arq.connections import RedisSettings

from app.config import settings


@lru_cache(maxsize=4)
def verified_context(cafile: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def http_verify() -> ssl.SSLContext | bool:
    return verified_context(settings.tls_ca_file) if settings.tls_ca_file else True


def redis_tls() -> dict[str, Any]:
    if settings.redis_url.startswith("rediss://"):
        return {
            "ssl_ca_certs": settings.tls_ca_file,
            "ssl_cert_reqs": "required",
            "ssl_check_hostname": True,
        }
    return {}


def queue_settings() -> RedisSettings:
    result = RedisSettings.from_dsn(settings.redis_url)
    if result.ssl:
        result.ssl_ca_certs = settings.tls_ca_file
        result.ssl_check_hostname = True
        result.ssl_cert_reqs = "required"
    return result


def websocket_tls(url: str) -> dict[str, Any]:
    if url.startswith("https://"):
        return {
            "ssl": verified_context(settings.tls_ca_file)
            if settings.tls_ca_file
            else ssl.create_default_context()
        }
    return {}
