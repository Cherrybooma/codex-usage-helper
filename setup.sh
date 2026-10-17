#!/bin/sh
# Prepare this checkout; never installs system tools or starts the application.
set -eu

usage() {
    cat <<'HELP'
Usage: sh setup.sh [--cli-only]

Create or validate .venv, then install optional compressed-log/timezone support.
On macOS, also build the desktop application unless --cli-only is supplied.
The application is not started, and Notion/Skill settings are not changed.

Requires Python 3.10+. Set USAGE_HELPER_PYTHON to an interpreter path to choose it.
Desktop builds require macOS 15+ and development tools with macOS 26+ SDK.
HELP
}

cli_only=false
for argument in "$@"; do
    case "$argument" in
        --help|-h) usage; exit 0 ;;
        --cli-only) cli_only=true ;;
        *) printf 'Unknown option: %s\n' "$argument" >&2; usage >&2; exit 2 ;;
    esac
done

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$cli_only" = false ] && [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'The desktop application requires macOS. Run sh setup.sh --cli-only for the scanner.' >&2
    exit 1
fi

supported_python() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

SELECTED_PYTHON=
if [ -n "${USAGE_HELPER_PYTHON:-}" ]; then
    if ! supported_python "$USAGE_HELPER_PYTHON"; then
        printf 'USAGE_HELPER_PYTHON must point to an executable Python 3.10+ interpreter: %s\n' "$USAGE_HELPER_PYTHON" >&2
        exit 1
    fi
    SELECTED_PYTHON=$USAGE_HELPER_PYTHON
else
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if candidate_path=$(command -v "$candidate" 2>/dev/null) && supported_python "$candidate_path"; then
            SELECTED_PYTHON=$candidate_path
            break
        fi
    done
fi
if [ -z "$SELECTED_PYTHON" ]; then
    printf '%s\n' 'Python 3.10+ was not found. Install it, then set USAGE_HELPER_PYTHON to its executable path and rerun setup.' >&2
    exit 1
fi

VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python3"
if [ -e "$VENV_DIR" ]; then
    if ! supported_python "$VENV_PYTHON" || ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' >/dev/null 2>&1; then
        printf '%s\n' 'The existing .venv is not a working Python 3.10+ virtual environment. Preserve or move it yourself, then rerun setup; it has not been deleted.' >&2
        exit 1
    fi
else
    printf 'Creating .venv with %s\n' "$SELECTED_PYTHON"
    "$SELECTED_PYTHON" -m venv "$VENV_DIR" || {
        printf '%s\n' 'Could not create .venv. Ensure this Python installation includes the venv module.' >&2
        exit 1
    }
fi

"$VENV_PYTHON" -m pip install -r "$PROJECT_DIR/requirements-optional.txt" || {
    printf '%s\n' 'Dependency installation failed. Check the network and Python pip installation, then rerun setup.' >&2
    exit 1
}
if [ "$cli_only" = false ]; then
    sh "$PROJECT_DIR/widget/build.sh"
    printf '%s\n' 'Setup complete. Start the desktop application with: sh widget/run.command'
else
    printf '%s\n' 'Setup complete. Run the scanner with: .venv/bin/python3 codex_usage.py --help'
fi
