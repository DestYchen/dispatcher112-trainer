#!/bin/sh
set -eu
umask 077
mkdir -p /tmp/dispatcher-redis
cp /run/tls/tls.key /tmp/dispatcher-redis/tls.key
chmod 600 /tmp/dispatcher-redis/tls.key
chown -R redis:redis /tmp/dispatcher-redis
case "$REDIS_PASSWORD" in ''|*[!A-Za-z0-9_-]*) echo 'Redis password must be a nonempty URL-safe value' >&2; exit 1 ;; esac
printf 'requirepass "%s"\n' "$REDIS_PASSWORD" > /tmp/dispatcher-redis/redis.conf
chown redis:redis /tmp/dispatcher-redis/redis.conf
exec /usr/local/bin/docker-entrypoint.sh redis-server /tmp/dispatcher-redis/redis.conf --appendonly yes \
  --port 0 --tls-port 6379 --tls-cert-file /run/tls/tls.crt \
  --tls-key-file /tmp/dispatcher-redis/tls.key --tls-ca-cert-file /run/tls/ca.crt \
  --tls-auth-clients no --tls-protocols 'TLSv1.2 TLSv1.3'
