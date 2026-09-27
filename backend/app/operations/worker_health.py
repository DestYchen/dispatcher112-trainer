"""Check the worker's expiring heartbeat without loading its job dependencies."""

import os

from redis import Redis
from redis.exceptions import RedisError


def healthy() -> bool:
    url = os.environ["REDIS_URL"]
    options = {"ssl_ca_certs": os.environ["TLS_CA_FILE"]} if url.startswith("rediss:") else {}
    with Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2, **options) as cache:
        # ARQ refreshes this key every five seconds, with a six-second expiry.
        return bool(cache.exists("arq:queue:health-check"))


if __name__ == "__main__":
    try:
        raise SystemExit(0 if healthy() else 1)
    except (KeyError, OSError, RedisError):
        raise SystemExit(1) from None
