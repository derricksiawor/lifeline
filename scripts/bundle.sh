#!/bin/bash
# Builds build/Lifeline.app from this folder. Every setting is optional:
#   LIFELINE_BUNDLE_ID      the app's bundle id (default com.example.lifeline)
#   LIFELINE_SIGN_IDENTITY  a code signing identity from your keychain; without one the app is signed ad hoc. With one
#                           the app also gets the hardened runtime
#   LIFELINE_TEAM_ID        your Team ID; with an identity it adds the desktop widgets, which share the app group
#                           TEAM_ID.BUNDLE_ID with the app
# --out FOLDER puts the build in FOLDER instead of build/ (Lifeline.app and the temporary files go there).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
package="$root/app"
out="$root/build"
if [ "${1:-}" = "--out" ]; then
    [ -n "${2:-}" ] || { echo "--out needs a folder." >&2; exit 1; }
    mkdir -p "$2"
    out="$(cd "$2" && pwd -P)"
fi
bundle="$out/Lifeline.app"
identifier="${LIFELINE_BUNDLE_ID:-com.example.lifeline}"
case "$identifier" in
    ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) echo "LIFELINE_BUNDLE_ID may use only letters, digits, dots, dashes and underscores, and starts with a letter or digit." >&2; exit 1 ;;
esac
identity="${LIFELINE_SIGN_IDENTITY:--}"
team="${LIFELINE_TEAM_ID:-}"
runtime=""
[ "$identity" = "-" ] || runtime="runtime"

swift build -c release --package-path "$package"
built="$(swift build -c release --package-path "$package" --show-bin-path)"

rm -rf "$bundle"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources/Fonts" "$bundle/Contents/Resources/core/core"
cp "$built/Lifeline" "$bundle/Contents/MacOS/Lifeline"
cp "$package/Resources/Info.plist" "$bundle/Contents/Info.plist"
cp "$package/Resources/Fonts/"*.ttf "$bundle/Contents/Resources/Fonts/"
cp "$package/Resources/AppIcon.icns" "$bundle/Contents/Resources/AppIcon.icns"
core="$bundle/Contents/Resources/core"
cp -R "$root/bin" "$core/bin"
cp -R "$root/core/lifeline" "$core/core/lifeline"
find "$core" -name __pycache__ -prune -exec rm -rf {} +
printf '%s\n' "$identifier" > "$core/core/lifeline/bundle-id.txt"

# The menu restarts a running service whose build differs from the one bundled here.
build="$(cd "$core" && find bin core -type f | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-64)"
printf '%s\n' "$build" > "$core/build.txt"

plist="$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $identifier" "$plist"
/usr/libexec/PlistBuddy -c "Add :LifelineServicePath string core/bin/lifeline" "$plist"
/usr/libexec/PlistBuddy -c "Add :LifelineServiceBuild string $build" "$plist"

if [ "$identity" != "-" ] && [ -n "$team" ]; then
    appex="$bundle/Contents/PlugIns/LifelineWidget.appex"
    mkdir -p "$appex/Contents/MacOS" "$appex/Contents/Resources/Fonts"
    cp "$built/LifelineWidget" "$appex/Contents/MacOS/LifelineWidget"
    cp "$package/Resources/LifelineWidget-Info.plist" "$appex/Contents/Info.plist"
    cp "$package/Resources/Fonts/"*.ttf "$appex/Contents/Resources/Fonts/"
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $identifier.widget" "$appex/Contents/Info.plist"
    group="$team.$identifier"
    entitlements() {
        printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        printf '<plist version="1.0"><dict>%s<key>com.apple.security.application-groups</key><array><string>%s</string></array></dict></plist>\n' "$1" "$group"
    }
    entitlements "<key>com.apple.security.app-sandbox</key><true/>" > "$out/widget.entitlements"
    entitlements "" > "$out/app.entitlements"
    codesign --force --sign "$identity" ${runtime:+--options "$runtime"} --entitlements "$out/widget.entitlements" "$appex"
    codesign --force --sign "$identity" ${runtime:+--options "$runtime"} --entitlements "$out/app.entitlements" "$bundle"
else
    codesign --force --sign "$identity" ${runtime:+--options "$runtime"} "$bundle"
fi
codesign --verify --deep --strict "$bundle"
echo "Built $bundle as $identifier."
