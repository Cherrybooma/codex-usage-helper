#!/bin/sh
set -eu
WIDGET_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$WIDGET_DIR")
APP_DIR="$WIDGET_DIR/build/用量小助手.app"
if [ ! -x "$APP_DIR/Contents/MacOS/UsageWidget" ]; then
  sh "$WIDGET_DIR/build.sh"
fi
# Starting from the project launcher keeps the authorized terminal context.
# LaunchServices can wait on Documents access after an ad-hoc rebuild.
if pgrep -x UsageWidget >/dev/null 2>&1; then
  open -a "$APP_DIR"
else
  nohup "$APP_DIR/Contents/MacOS/UsageWidget" "$PROJECT_DIR" >/dev/null 2>&1 &
fi
