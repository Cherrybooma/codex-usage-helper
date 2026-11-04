#!/bin/sh
set -eu
PM_SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$PM_SOURCE_DIR/pm" check
