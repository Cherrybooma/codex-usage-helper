#!/bin/sh
set -eu
PM_SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$PM_SOURCE_DIR/.venv/bin/python3" "$PM_SOURCE_DIR/widget/service_control.py" start "$@"
