#!/bin/sh
set -eu
WIDGET_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$WIDGET_DIR")
APP_DIR="$WIDGET_DIR/build/用量小助手.app"
if [ ! -x "$APP_DIR/Contents/MacOS/UsageWidget" ]; then
  sh "$WIDGET_DIR/build.sh"
fi
open -a "$APP_DIR" --args "$PROJECT_DIR" --settings
