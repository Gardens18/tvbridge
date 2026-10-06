#!/usr/bin/env bash
#
# tvbridge uninstaller: stops and removes the two LaunchAgents. Keeps all data.
#
#   ./uninstall.sh
#
# Removes:  ~/Library/LaunchAgents/com.tvbridge.engine.plist
#           ~/Library/LaunchAgents/com.tvbridge.ngrok.plist
# Keeps:    $TVBRIDGE_HOME (config, calibration, database, screenshots, ngrok binary),
#           ~/Library/Logs/tvbridge and the .venv next to this script.
#
# Open positions in MetaTrader 5 are NOT closed by this script. Never uses sudo.
# Safe to run more than once.

set -euo pipefail

ENGINE_LABEL="com.tvbridge.engine"
NGROK_LABEL="com.tvbridge.ngrok"

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh

Stops tvbridge (engine + ngrok tunnel) and removes its LaunchAgents from
~/Library/LaunchAgents. Your config, calibration, database, screenshots and logs
are kept. Open positions in MetaTrader 5 are not touched.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *)         printf 'unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -eq 0 ]; then
    printf 'error: do not run uninstall.sh as root or with sudo.\n' >&2
    exit 1
fi

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TVBRIDGE_HOME="${TVBRIDGE_HOME:-$HOME/.tvbridge}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs/tvbridge"
GUI_DOMAIN="gui/$(id -u)"

# Stop the tunnel first so no new webhooks arrive, then the engine.
for label in "$NGROK_LABEL" "$ENGINE_LABEL"; do
    plist="$AGENTS_DIR/$label.plist"
    if launchctl print "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
        if launchctl bootout "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
            printf '    stopped   %s\n' "$label"
        else
            printf '    warning: launchctl bootout %s/%s failed (already stopped?)\n' "$GUI_DOMAIN" "$label" >&2
        fi
    else
        printf '    not loaded %s\n' "$label"
    fi
    if [ -f "$plist" ]; then
        rm -f "$plist"
        printf '    removed   %s\n' "$plist"
    fi
done

cat <<EOF

tvbridge LaunchAgents removed. Nothing else was deleted.

  Open positions in MetaTrader 5 were NOT closed; they keep their server-side SL/TP.

  Kept (delete by hand only if you are sure you no longer need them):
    data:   $TVBRIDGE_HOME
    logs:   $LOGDIR
    venv:   $APPDIR/.venv

  To reinstall later:  cd "$APPDIR" && ./install.sh
EOF
