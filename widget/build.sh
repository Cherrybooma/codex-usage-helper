#!/bin/sh
set -eu
WIDGET_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$WIDGET_DIR")
APP_DIR="$WIDGET_DIR/build/用量小助手.app"
if [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'The desktop application can only be built on macOS.' >&2
    exit 1
fi
if ! SDK_VERSION=$(xcrun --sdk macosx --show-sdk-version 2>/dev/null); then
    printf '%s\n' 'macOS SDK not found. Install/select Xcode or Command Line Tools with macOS 26+ SDK.' >&2
    exit 1
fi
SDK_MAJOR=${SDK_VERSION%%.*}
case "$SDK_MAJOR" in
    ''|*[!0-9]*) printf 'Unrecognized macOS SDK version: %s\n' "$SDK_VERSION" >&2; exit 1 ;;
esac
if [ "$SDK_MAJOR" -lt 26 ]; then
    printf 'macOS 26+ SDK is required for Liquid Glass APIs; selected SDK is %s. Install/select newer development tools.\n' "$SDK_VERSION" >&2
    exit 1
fi
ARCHITECTURE=$(uname -m)
case "$ARCHITECTURE" in
    arm64|x86_64) ;;
    *) printf 'Unsupported build architecture: %s\n' "$ARCHITECTURE" >&2; exit 1 ;;
esac
mkdir -p "$APP_DIR/Contents/MacOS" "$PROJECT_DIR/widget/build/module-cache"
xcrun swiftc -parse-as-library -swift-version 5 -target "$ARCHITECTURE-apple-macosx15.0" "$WIDGET_DIR/UsageWidget.swift" "$WIDGET_DIR/UsageStrip.swift" "$WIDGET_DIR/NotionSettings.swift" -o "$APP_DIR/Contents/MacOS/UsageWidget" -framework AppKit -framework SwiftUI -framework Security -module-cache-path "$WIDGET_DIR/build/module-cache"
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>UsageWidget</string>
<key>CFBundleIdentifier</key><string>local.codex.usage-widget</string>
<key>CFBundleName</key><string>用量小助手</string>
<key>CFBundleShortVersionString</key><string>0.2.0</string>
<key>CFBundleVersion</key><string>2</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
<key>LSMinimumSystemVersion</key><string>15.0</string>
</dict></plist>
PLIST
# A local ad-hoc signature is sufficient; this is not a published application.
codesign --force --sign - "$APP_DIR"
printf '%s\n' "$APP_DIR"
