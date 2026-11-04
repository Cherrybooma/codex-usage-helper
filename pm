#!/bin/sh
# Use this checkout's program and data with the existing central management vault.
set -eu
PM_SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PM_VAULT_DIR=${PROJECT_PM_VAULT:-"$HOME/Documents/项目管理obsidian"}
PM_PYTHON="$PM_SOURCE_DIR/.venv/bin/python3"
if [ ! -x "$PM_PYTHON" ]; then
    printf '%s\n' '请先在用量小助手源码目录运行 sh setup.sh --cli-only 建立本机运行环境。' >&2
    exit 1
fi
exec "$PM_PYTHON" "$PM_SOURCE_DIR/widget/project_manager.py" --vault "$PM_VAULT_DIR" "$@"
