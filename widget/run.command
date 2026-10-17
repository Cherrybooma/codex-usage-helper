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
WIDGET_PYTHON="$PROJECT_DIR/.venv/bin/python3"
if [ ! -x "$WIDGET_PYTHON" ]; then
  WIDGET_PYTHON=$(command -v python3)
fi
"$WIDGET_PYTHON" - "$APP_DIR" "$PROJECT_DIR" <<'PY'
from pathlib import Path
import subprocess
import sys
import time

app, root = Path(sys.argv[1]), sys.argv[2]
executable = str(app / "Contents/MacOS/UsageWidget")
processes = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, check=True)
running = any(len(parts := line.strip().split(None, 1)) == 2 and parts[1] == executable
              for line in processes.stdout.splitlines())
if running:
    subprocess.run(["open", "-a", str(app)], check=True)
else:
    # The desktop component must outlive the launcher/terminal process group.
    process = subprocess.Popen([executable, root], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True)
    time.sleep(.25)
    if process.poll() is not None:
        raise SystemExit("组件未能启动，请重新构建后重试。")
PY
