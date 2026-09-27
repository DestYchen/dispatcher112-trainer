#!/bin/sh
set -eu
install -m 600 -o postgres -g postgres /run/tls/tls.key /tmp/dispatcher-postgres.key
exec /usr/local/bin/docker-entrypoint.sh postgres \
  -c ssl=on -c ssl_min_protocol_version=TLSv1.2 \
  -c ssl_cert_file=/run/tls/tls.crt -c ssl_key_file=/tmp/dispatcher-postgres.key \
  -c ssl_ca_file=/run/tls/ca.crt -c hba_file=/etc/postgresql/tls_hba.conf
