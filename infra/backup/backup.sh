#!/bin/sh
set -eu
exec python3 /scripts/full_backup.py "$@"
