#!/usr/bin/env bash
# Daily production entry point: encrypted complete backup and failure alert.
set -euo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { echo 'Backup wrapper must run through sudo -n.' >&2; exit 2; }
[[ $# -eq 0 ]] || { echo 'Backup wrapper accepts no arguments.' >&2; exit 2; }
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

exec /opt/rezo/scripts/notify_cron.sh --name taxi-core-full-backup -- \
  /usr/bin/flock -n /run/lock/rezo-taxi-core-backup.lock \
  /usr/bin/python3 /opt/rezo-taxi-core/deploy/backup_taxi_core.py \
  --output-dir /var/backups/rezo-taxi-core/full --local-only
