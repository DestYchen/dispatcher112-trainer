#!/bin/sh
set -eu
umask 077
if [ "$#" -gt 0 ]; then exec "$@"; fi
exec python3 -m app.operations.backup_worker
