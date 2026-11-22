#!/usr/bin/env bash
# Sets up Lifeline on another computer, Linux or Mac, so the Mac's menu can read the accounts Claude Code uses there.
# Run it as your own user from a copy of this folder on that computer: bash devbox/install.sh
# It prints this computer's address and a one-time pairing code to enter in the Mac's menu. Lifeline listens on that
# address only.
# bash devbox/install.sh --listen ADDRESS listens on another address, such as a VPN address. --listen 0.0.0.0 opens the
# port on every network this computer joins.
# bash devbox/install.sh --port 7500 listens on that port instead of 7433.
# bash devbox/install.sh --remove stops and removes the service. Your accounts and sign-ins stay.
# Everything it creates is named from LIFELINE_BUNDLE_ID (default com.example.lifeline), so it never touches another
# Lifeline on the same computer: the unit <id>.service, the launchd label <id>.remote, and the folder ~/.local/share/<id>.
set -euo pipefail
umask 077
shopt -u patsub_replacement 2>/dev/null || true
# Paths go into the plist as XML text and into the unit as quoted words, so a home folder with any name works.
xml() { local s="${1//&/&amp;}"; s="${s//</&lt;}"; printf '%s' "${s//>/&gt;}"; }
word() { local s="${1//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//%/%%}"; printf '"%s"' "${s//\$/\$\$}"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$(dirname "$HERE")"
ID="${LIFELINE_BUNDLE_ID:-com.example.lifeline}"
case "$ID" in
    ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) echo "LIFELINE_BUNDLE_ID may use only letters, digits, dots, dashes and underscores, and starts with a letter or digit." >&2; exit 1 ;;
esac
PORT=7433
LISTEN=""
REMOVE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --remove) REMOVE=1; shift ;;
        --port) PORT="${2:-}"; shift 2 || shift ;;
        --listen) LISTEN="${2:-}"; shift 2 || shift ;;
        *) echo "Unknown option $1." >&2; exit 1 ;;
    esac
done
case "$LISTEN" in
    *[!A-Za-z0-9.-]*) echo "--listen needs one IPv4 address or host name, such as 192.168.1.20, or 0.0.0.0 for every network." >&2; exit 1 ;;
esac
case "$PORT" in
    ""|*[!0-9]*) echo "--port needs a number from 1 to 65535." >&2; exit 1 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "--port needs a number from 1 to 65535." >&2
    exit 1
fi
APP="$HOME/.local/share/$ID/app"
LABEL="$ID.remote"
UNIT="$HOME/.config/systemd/user/$ID.service"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
PATHS="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your own user, not as root." >&2
    exit 1
fi

stop_service() {
    if [ "$(uname)" = "Darwin" ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    else
        systemctl --user disable --now "$ID.service" 2>/dev/null || true
    fi
}

if [ -n "$REMOVE" ]; then
    stop_service
    rm -f "$AGENT" "$UNIT"
    [ "$(uname)" = "Darwin" ] || systemctl --user daemon-reload
    echo "Lifeline no longer runs on this computer. Your accounts and sign-ins are still in place."
    exit 0
fi

python=""
for candidate in python3.14 python3.13 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
        python="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$python" ]; then
    echo "Lifeline needs Python 3.13 or newer on this computer." >&2
    exit 1
fi
claude="$(PATH="$PATHS:$PATH" command -v claude || true)"
if [ -z "$claude" ]; then
    echo "Claude Code's claude command is not installed here, so Lifeline can read accounts but cannot sign in to new ones." >&2
fi

stop_service
rm -rf "$APP"
mkdir -p "$APP/core"
cp -R "$SRC/bin" "$APP/bin"
cp -R "$SRC/core/lifeline" "$APP/core/lifeline"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
printf '%s\n' "$ID" > "$APP/core/lifeline/bundle-id.txt"
printf '%s\n' "$PORT" > "$APP/core/lifeline/port.txt"
if [ -z "$LISTEN" ]; then
    LISTEN="$("$python" -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from lifeline import pairing; print(pairing.own_address())' "$APP/core")"
fi
printf '%s\n' "$LISTEN" > "$APP/core/lifeline/listen.txt"
[ -z "$claude" ] || printf '%s\n' "$claude" > "$APP/core/lifeline/claude.txt"
# lifeline pair and lifeline pair --reset run through this, with the Python found above.
printf '#!/bin/bash\nexec %q -I -B %q "$@"\n' "$python" "$APP/bin/lifeline" > "$APP/lifeline"
chmod 700 "$APP/lifeline"
run=("$APP/lifeline")

if [ "$(uname)" = "Darwin" ]; then
    mkdir -p "$(dirname "$AGENT")" "$HOME/Library/Logs/$ID"
    cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$(xml "$LABEL")</string>
    <key>ProgramArguments</key>
    <array><string>$(xml "$python")</string><string>-I</string><string>-B</string><string>$(xml "$APP/bin/lifeline")</string><string>serve</string><string>--remote</string></array>
    <key>EnvironmentVariables</key><dict><key>PATH</key><string>$(xml "$PATHS")</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardErrorPath</key><string>$(xml "$HOME/Library/Logs/$ID/remote.log")</string>
</dict>
</plist>
EOF
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
else
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<EOF
[Unit]
Description=Lifeline for the Mac's menu

[Service]
ExecStart=/usr/bin/env $(word "PATH=$PATHS") $(word "$python") -I -B $(word "$APP/bin/lifeline") serve --remote
UMask=0077
Restart=on-failure
RestartSec=10
MemoryMax=150M
OOMPolicy=continue
NoNewPrivileges=yes

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$ID.service"
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" != "yes" ]; then
        echo "Run sudo loginctl enable-linger $(id -un) so Lifeline keeps running after you log out."
    fi
fi

"${run[@]}" pair
