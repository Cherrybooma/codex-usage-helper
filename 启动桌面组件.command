#!/bin/sh
set -eu
PM_SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# The capsule reads Markdown directly; no HTTP service is required.
exec sh "$PM_SOURCE_DIR/widget/run.command"
