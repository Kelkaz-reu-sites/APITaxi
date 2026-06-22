#!/bin/sh

LOCK_DIR=/venv/.install-lock

while ! sudo -E mkdir "$LOCK_DIR" 2>/dev/null; do
  sleep 1
done

cleanup_lock() {
  sudo -E rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT

if [ ! -x /venv/bin/python ]; then
  sudo -E python3 -m venv /venv
fi

. /venv/bin/activate

sudo -E /venv/bin/pip install tox "watchdog[watchmedo]>=6.0.0" pytest flake8 pylint

test -d "/git/APITaxi" && sudo -E /venv/bin/pip install -e "/git/APITaxi"

trap - EXIT
cleanup_lock

# Execute Docker CMD
exec "$@"
