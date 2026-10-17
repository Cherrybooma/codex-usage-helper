#!/bin/sh
# Archive a committed source tree, excluding files marked export-ignore.
set -eu

usage() {
    cat <<'HELP'
Usage: sh scripts/package-source.sh [REF]

Package the commit selected by REF (default HEAD) into dist/ as a source ZIP.
Only committed files are included; .gitattributes export-ignore rules apply.
Uncommitted edits, local data, virtual environments and build outputs are not
added. The output filename contains the commit ID and is never overwritten.
HELP
}

case "${1:-}" in --help|-h) usage; exit 0 ;; esac
if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
reference=${1:-HEAD}
if ! commit=$(git -C "$PROJECT_DIR" rev-parse --verify --end-of-options "${reference}^{commit}" 2>/dev/null); then
    printf 'Not a valid committed Git reference: %s\n' "$reference" >&2
    exit 1
fi
short_commit=$(git -C "$PROJECT_DIR" rev-parse --short=12 "$commit")
archive_name="codex-usage-helper-${short_commit}-source.zip"
archive_path="$PROJECT_DIR/dist/$archive_name"
if git -C "$PROJECT_DIR" ls-files --error-unmatch -- "dist/$archive_name" >/dev/null 2>&1; then
    printf 'Refusing to overwrite a tracked file: %s\n' "$archive_path" >&2
    exit 1
fi
if [ -e "$archive_path" ]; then
    printf 'Archive already exists; it was not overwritten: %s\n' "$archive_path" >&2
    exit 1
fi
mkdir -p "$PROJECT_DIR/dist"
temporary=$(mktemp "$PROJECT_DIR/dist/.source-package.XXXXXX")
trap 'rm -f "$temporary"' 0 HUP INT TERM
git -C "$PROJECT_DIR" archive --format=zip --prefix=codex-usage-helper/ "$commit" > "$temporary"
mv "$temporary" "$archive_path"
printf 'Source commit: %s\n%s\n' "$commit" "$archive_path"
