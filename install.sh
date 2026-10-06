#!/usr/bin/env bash
#
# tvbridge installer (macOS, per-user, never needs sudo).
#
#   ./install.sh [--no-launchd] [--no-ngrok]
#
# What it does, in order (safe to re-run at any time; every step is idempotent):
#   1. creates .venv next to this script with /usr/bin/python3 and installs requirements.txt
#   2. runs "python -m tvbridge init" (creates $TVBRIDGE_HOME and config.json if absent)
#   3. downloads the ngrok agent into $TVBRIDGE_HOME/bin/ngrok if it is not there yet
#      and, once ngrok.authtoken + ngrok.domain are set in config.json, writes ngrok.yml
#   4. renders the LaunchAgents in launchd/ into ~/Library/LaunchAgents, checks them with
#      plutil -lint and (re)loads them into your GUI session with launchctl
#
# It never touches MetaTrader 5, never places orders and never changes system settings.
# The app folder may contain spaces: every path below is quoted.

set -euo pipefail

ENGINE_LABEL="com.tvbridge.engine"
NGROK_LABEL="com.tvbridge.ngrok"
NGROK_URL_BASE="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin"

# ----------------------------------------------------------------------------- output

if [ -t 1 ]; then
    C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_OFF=""
fi

step() { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %sok%s  %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '    %swarning:%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '\n%serror:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: ./install.sh [--no-launchd] [--no-ngrok]

  --no-launchd   do not install/load the LaunchAgents (run "python -m tvbridge run" yourself)
  --no-ngrok     do not download ngrok, write ngrok.yml or load the ngrok LaunchAgent
  -h, --help     show this help

Environment:
  TVBRIDGE_HOME  data directory (default: ~/.tvbridge)

Safe to re-run. Never uses sudo. Never touches MetaTrader 5.
EOF
}

# ----------------------------------------------------------------------------- arguments

WITH_LAUNCHD=1
WITH_NGROK=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-launchd) WITH_LAUNCHD=0 ;;
        --no-ngrok)   WITH_NGROK=0 ;;
        -h|--help)    usage; exit 0 ;;
        *)            printf 'unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# ----------------------------------------------------------------------------- cleanup / errors

DOWNLOAD_TMP=""
cleanup() {
    if [ -n "${DOWNLOAD_TMP:-}" ] && [ -d "$DOWNLOAD_TMP" ]; then
        rm -rf "$DOWNLOAD_TMP"
    fi
}
trap cleanup EXIT
trap 'printf "\n%sinstall.sh failed near line %s.%s Fix the problem shown above and re-run ./install.sh (it is safe to re-run).\n" "$C_RED" "$LINENO" "$C_OFF" >&2' ERR

# ----------------------------------------------------------------------------- paths

[ "$(uname -s)" = "Darwin" ] || die "tvbridge only runs on macOS."
if [ "$(id -u)" -eq 0 ]; then
    die "do not run install.sh as root or with sudo. Run it as the macOS user who is logged in to the desktop where MetaTrader 5 runs."
fi

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="$APPDIR/.venv"
PY="$VENV/bin/python"
REQUIREMENTS="$APPDIR/requirements.txt"

TVBRIDGE_HOME="${TVBRIDGE_HOME:-$HOME/.tvbridge}"
case "$TVBRIDGE_HOME" in
    "~")    TVBRIDGE_HOME="$HOME" ;;
    "~/"*)  TVBRIDGE_HOME="$HOME/${TVBRIDGE_HOME#\~/}" ;;
esac
case "$TVBRIDGE_HOME" in
    /*) ;;
    *)  die "TVBRIDGE_HOME must be an absolute path (got: $TVBRIDGE_HOME)" ;;
esac
while [ "${#TVBRIDGE_HOME}" -gt 1 ] && [ "${TVBRIDGE_HOME%/}" != "$TVBRIDGE_HOME" ]; do
    TVBRIDGE_HOME="${TVBRIDGE_HOME%/}"
done
export TVBRIDGE_HOME

CONFIG="$TVBRIDGE_HOME/config.json"
NGROK_BIN="$TVBRIDGE_HOME/bin/ngrok"
LOGDIR="$HOME/Library/Logs/tvbridge"
AGENTS_DIR="$HOME/Library/LaunchAgents"
GUI_DOMAIN="gui/$(id -u)"

# ----------------------------------------------------------------------------- helpers

# render_template <template> <output> KEY=VALUE...
# Replaces every __KEY__ placeholder in <template> with VALUE (XML-escaped) and writes
# <output> atomically with mode 0644. Fails if the template contains a placeholder that
# has no value, or a value is empty. Done in Python, not sed: paths contain spaces and
# slashes, and plist values must be XML-escaped.
render_template() {
    "$PY" - "$@" <<'RENDER_PY'
import os
import re
import sys
import tempfile
from xml.sax.saxutils import escape

if len(sys.argv) < 3:
    sys.exit("render_template: usage: <template> <output> KEY=VALUE...")
template, output = sys.argv[1], sys.argv[2]
values = {}
for pair in sys.argv[3:]:
    key, sep, value = pair.partition("=")
    if not sep or not re.match(r"^[A-Z][A-Z_]*$", key):
        sys.exit("render_template: bad KEY=VALUE argument: %r" % pair)
    values["__%s__" % key] = value

with open(template, "r", encoding="utf-8") as fh:
    text = fh.read()

pattern = re.compile(r"__[A-Z][A-Z_]*?__")
used = set(pattern.findall(text))
missing = sorted(p for p in used if p not in values)
if missing:
    sys.exit("render_template: %s has no value for %s" % (template, ", ".join(missing)))
empty = sorted(p for p in used if not values[p].strip())
if empty:
    sys.exit("render_template: empty value for %s" % ", ".join(empty))

# Single pass, so a value that happens to contain "__X__" is never substituted again.
rendered = pattern.sub(lambda m: escape(values[m.group(0)]), text)

out_dir = os.path.dirname(os.path.abspath(output))
fd, tmp_path = tempfile.mkstemp(prefix=".tvbridge-render-", dir=out_dir)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    os.chmod(tmp_path, 0o644)  # launchd refuses agents that are group/world writable
    os.replace(tmp_path, output)
except BaseException:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
    raise
RENDER_PY
}

# config_value <get|configured> <dotted.key> [default]
#   get         prints the value at dotted.key in config.json (or default when missing/empty)
#   configured  prints "yes" if the value is a non-empty, non-placeholder string, else "no"
# Uses the json module (no jq). Never prints secrets in "configured" mode.
config_value() {
    "$PY" - "$CONFIG" "$@" <<'CONFIG_PY'
import json
import sys

path, mode, key = sys.argv[1], sys.argv[2], sys.argv[3]
default = sys.argv[4] if len(sys.argv) > 4 else ""
try:
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
except ValueError as exc:
    sys.exit("%s is not valid JSON: %s" % (path, exc))

node = data
for part in key.split("."):
    if isinstance(node, dict) and part in node:
        node = node[part]
    else:
        node = None
        break

if mode == "configured":
    text = node.strip() if isinstance(node, str) else ""
    lowered = text.lower()
    placeholder_marks = ("<", ">", "your-name", "your_", "yourtoken", "changeme",
                         "change_me", "replace", "xxxx", "...")
    looks_placeholder = any(mark in lowered for mark in placeholder_marks)
    print("yes" if text and not looks_placeholder else "no")
elif mode == "get":
    if node is None or node == "":
        print(default)
    elif isinstance(node, bool):
        print("true" if node else "false")
    else:
        print(node)
else:
    sys.exit("config_value: unknown mode %r" % mode)
CONFIG_PY
}

# load_agent <label> <plist>: bootout (ignore failure), enable, bootstrap with retries.
load_agent() {
    local label="$1" plist="$2" err="" attempt
    launchctl bootout "$GUI_DOMAIN/$label" >/dev/null 2>&1 || true
    launchctl enable "$GUI_DOMAIN/$label" >/dev/null 2>&1 || true
    # bootout is asynchronous; bootstrap right after it can fail with "5: Input/output error".
    for attempt in 1 2 3 4 5 6; do
        if err="$(launchctl bootstrap "$GUI_DOMAIN" "$plist" 2>&1)"; then
            ok "loaded $label"
            return 0
        fi
        sleep 1
    done
    die "launchctl bootstrap $GUI_DOMAIN \"$plist\" failed: $err
       Are you running this from the logged-in desktop session (not over SSH)?"
}

# install_plist <template> <label> KEY=VALUE...: render, lint, load.
install_plist() {
    local template="$1" label="$2"
    shift 2
    local plist="$AGENTS_DIR/$label.plist"
    [ -f "$template" ] || die "missing template: $template"
    render_template "$template" "$plist" "$@"
    if ! plutil -lint "$plist" >/dev/null; then
        plutil -lint "$plist" >&2 || true
        die "rendered $plist failed plutil -lint; not loading it"
    fi
    ok "wrote $plist (plutil -lint OK)"
    load_agent "$label" "$plist"
}

ngrok_arch() {
    local machine
    machine="$(uname -m)"
    case "$machine" in
        arm64)  echo "arm64" ;;
        x86_64) echo "amd64" ;;
        *)      die "unsupported CPU architecture for ngrok: $machine" ;;
    esac
}

install_ngrok_binary() {
    if [ -x "$NGROK_BIN" ] && "$NGROK_BIN" version >/dev/null 2>&1; then
        ok "ngrok already installed: $("$NGROK_BIN" version 2>/dev/null) ($NGROK_BIN)"
        return 0
    fi
    local arch url
    arch="$(ngrok_arch)"
    url="$NGROK_URL_BASE-$arch.zip"
    mkdir -p "$TVBRIDGE_HOME/bin"
    # Download and unzip into a staging dir inside $TVBRIDGE_HOME/bin, then move into place,
    # so an interrupted download never leaves a broken bin/ngrok behind.
    DOWNLOAD_TMP="$(mktemp -d "$TVBRIDGE_HOME/bin/.ngrok-download.XXXXXX")"
    info "downloading $url"
    curl -fsSL --retry 3 --connect-timeout 20 -o "$DOWNLOAD_TMP/ngrok.zip" "$url" \
        || die "ngrok download failed ($url). Re-run ./install.sh, or use --no-ngrok and install ngrok yourself into $NGROK_BIN"
    unzip -o -q "$DOWNLOAD_TMP/ngrok.zip" -d "$DOWNLOAD_TMP/unzipped" \
        || die "could not unzip the ngrok download"
    [ -f "$DOWNLOAD_TMP/unzipped/ngrok" ] || die "the ngrok zip did not contain an 'ngrok' binary"
    chmod 755 "$DOWNLOAD_TMP/unzipped/ngrok"
    mv -f "$DOWNLOAD_TMP/unzipped/ngrok" "$NGROK_BIN"
    rm -rf "$DOWNLOAD_TMP"
    DOWNLOAD_TMP=""
    if "$NGROK_BIN" version >/dev/null 2>&1; then
        ok "installed $("$NGROK_BIN" version 2>/dev/null) -> $NGROK_BIN"
    else
        die "$NGROK_BIN does not run (wrong architecture? uname -m = $(uname -m))"
    fi
}

# ----------------------------------------------------------------------------- 1. preflight

step "Checking prerequisites"
info "app folder:    $APPDIR"
info "data folder:   $TVBRIDGE_HOME"
info "logs:          $LOGDIR"
[ -f "$APPDIR/tvbridge/__main__.py" ] || die "tvbridge/__main__.py not found next to install.sh; run install.sh from the tvbridge folder."
[ -f "$REQUIREMENTS" ] || die "requirements.txt not found in $APPDIR"
if ! xcode-select -p >/dev/null 2>&1; then
    die "Apple's Command Line Tools are not installed (they provide /usr/bin/python3).
       Run:  xcode-select --install   then re-run ./install.sh"
fi
[ -x /usr/bin/python3 ] || die "/usr/bin/python3 not found"
if ! /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    die "/usr/bin/python3 is missing or older than 3.9 (run: xcode-select --install)"
fi
ok "/usr/bin/python3 $(/usr/bin/python3 -c 'import platform; print(platform.python_version())')"

# ----------------------------------------------------------------------------- 2. venv + deps

step "Python virtual environment"
if [ -d "$VENV" ] && ! "$PY" -c 'import sys' >/dev/null 2>&1; then
    # Typically a .venv copied from another Mac whose python path does not exist here.
    warn "existing $VENV does not run here; recreating it"
    /usr/bin/python3 -m venv --clear "$VENV"
    ok "recreated $VENV"
elif [ ! -x "$PY" ]; then
    /usr/bin/python3 -m venv "$VENV"
    ok "created $VENV"
else
    ok "reusing $VENV"
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "$PY is older than Python 3.9; delete $VENV and re-run ./install.sh"

info "installing requirements (pyobjc) ..."
"$PY" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$PY" -m pip install --disable-pip-version-check --quiet -r "$REQUIREMENTS"
"$PY" -c 'import Quartz, Vision, AppKit, ApplicationServices' \
    || die "pyobjc frameworks failed to import after pip install"
ok "requirements installed"

# ----------------------------------------------------------------------------- 3. data dir + config

step "Data folder and config"
mkdir -p "$TVBRIDGE_HOME" "$TVBRIDGE_HOME/shots" "$LOGDIR"
chmod 700 "$TVBRIDGE_HOME"
( cd "$APPDIR" && "$PY" -m tvbridge init )
[ -f "$CONFIG" ] || die "python -m tvbridge init did not create $CONFIG"
chmod 600 "$CONFIG"
ok "config: $CONFIG"

SERVER_PORT="$(config_value get server.port 8787)"
case "$SERVER_PORT" in
    ''|*[!0-9]*) die "server.port in $CONFIG must be a number (got: $SERVER_PORT)" ;;
esac
SERVER_PATH="$(config_value get server.path /webhook)"
EXEC_MODE="$(config_value get executor.mode paper)"

# ----------------------------------------------------------------------------- 4. ngrok

NGROK_READY=0
NGROK_DOMAIN=""
NGROK_SKIP_REASON=""
if [ "$WITH_NGROK" -eq 1 ]; then
    step "ngrok"
    install_ngrok_binary
    TOKEN_SET="$(config_value configured ngrok.authtoken)"
    DOMAIN_SET="$(config_value configured ngrok.domain)"
    NGROK_DOMAIN="$(config_value get ngrok.domain)"
    NGROK_DOMAIN="${NGROK_DOMAIN%/}"   # config.py also tolerates one trailing slash
    if [ "$TOKEN_SET" != "yes" ] || [ "$DOMAIN_SET" != "yes" ]; then
        NGROK_SKIP_REASON="ngrok.authtoken and/or ngrok.domain are not set in $CONFIG yet"
    else
        case "$NGROK_DOMAIN" in
            *://*|*/*|*:*)
                NGROK_SKIP_REASON="ngrok.domain must be a bare host name such as your-name.ngrok-free.app (no https://, no path, no port); got: $NGROK_DOMAIN" ;;
            *[!A-Za-z0-9.-]*)
                NGROK_SKIP_REASON="ngrok.domain contains characters that are not valid in a host name: $NGROK_DOMAIN" ;;
            *)
                ( cd "$APPDIR" && "$PY" -m tvbridge ngrok-config )
                [ -f "$TVBRIDGE_HOME/ngrok.yml" ] || die "python -m tvbridge ngrok-config did not create $TVBRIDGE_HOME/ngrok.yml"
                chmod 600 "$TVBRIDGE_HOME/ngrok.yml"
                NGROK_READY=1
                ok "ngrok.yml written for https://$NGROK_DOMAIN" ;;
        esac
    fi
    if [ "$NGROK_READY" -ne 1 ]; then
        warn "skipping the ngrok tunnel: $NGROK_SKIP_REASON"
        info "Set them (dashboard.ngrok.com -> Your Authtoken / Domains), then re-run ./install.sh"
    fi
fi

# ----------------------------------------------------------------------------- 5. launchd

if [ "$WITH_LAUNCHD" -eq 1 ]; then
    step "LaunchAgents ($GUI_DOMAIN)"
    mkdir -p "$AGENTS_DIR" "$LOGDIR"
    install_plist "$APPDIR/launchd/$ENGINE_LABEL.plist.template" "$ENGINE_LABEL" \
        "PYTHON=$PY" \
        "APPDIR=$APPDIR" \
        "TVBRIDGE_HOME=$TVBRIDGE_HOME" \
        "LOGDIR=$LOGDIR"
    if [ "$WITH_NGROK" -eq 1 ] && [ "$NGROK_READY" -eq 1 ]; then
        install_plist "$APPDIR/launchd/$NGROK_LABEL.plist.template" "$NGROK_LABEL" \
            "NGROK=$NGROK_BIN" \
            "PORT=$SERVER_PORT" \
            "DOMAIN=$NGROK_DOMAIN" \
            "TVBRIDGE_HOME=$TVBRIDGE_HOME" \
            "LOGDIR=$LOGDIR"
    elif [ "$WITH_NGROK" -eq 1 ]; then
        warn "ngrok LaunchAgent NOT installed: $NGROK_SKIP_REASON"
        if [ -f "$AGENTS_DIR/$NGROK_LABEL.plist" ]; then
            info "an older $AGENTS_DIR/$NGROK_LABEL.plist exists and was left untouched"
        fi
    else
        info "--no-ngrok: ngrok LaunchAgent left untouched"
    fi
else
    step "LaunchAgents"
    info "--no-launchd: nothing loaded. Start the engine yourself with:"
    info "  cd \"$APPDIR\" && .venv/bin/python -m tvbridge run"
fi

# ----------------------------------------------------------------------------- 6. next steps

TV="cd \"$APPDIR\" && .venv/bin/python -m tvbridge"
step "Done. Next steps (see README.md for the full manual)"
cat <<EOF

  Current mode: executor.mode = "$EXEC_MODE"   (start in "paper"; you switch modes yourself)

  1. Edit the config (keep executor.mode = "paper" for now):
       open -e "$CONFIG"
     - ngrok.authtoken and ngrok.domain   (dashboard.ngrok.com: "Your Authtoken", "Domains")
     - account.account_login and account.server_name (exactly as in the MetaTrader 5 window title)
     Then re-run ./install.sh so the ngrok tunnel gets installed.

  2. Check everything and trigger the macOS permission prompts:
       $TV doctor --prompt

  3. System Settings -> Privacy & Security -> Accessibility AND Screen Recording:
     add the exact python binary that doctor prints (see README "Permissions"),
     then restart the engine:
       launchctl kickstart -k $GUI_DOMAIN/$ENGINE_LABEL

  4. In MetaTrader 5, logged in to a DEMO account, calibrate (you only hover, nothing is clicked):
       $TV calibrate

  5. Rehearse (fills the order ticket, verifies it with OCR and cancels it; DEMO first):
       $TV rehearse --symbol EURUSD --side buy --sl <price below market>

  6. While executor.mode is still "paper", test the whole chain without TradingView
     (send-test goes through the real pipeline: in "live" mode it would really trade):
       $TV send-test --action buy --symbol EURUSD --price <price> --sl <stop>
       $TV status

  7. Only after paper and rehearsal look right, set executor.mode yourself
     (paper -> rehearsal -> live) in config.json and restart the engine:
       launchctl kickstart -k $GUI_DOMAIN/$ENGINE_LABEL

  Logs:        $LOGDIR
  Screenshots: $TVBRIDGE_HOME/shots
EOF
if [ "$NGROK_READY" -eq 1 ]; then
    printf '\n  TradingView webhook URL:  https://%s%s\n' "$NGROK_DOMAIN" "$SERVER_PATH"
fi
if [ "$TVBRIDGE_HOME" != "$HOME/.tvbridge" ]; then
    printf '\n  Note: TVBRIDGE_HOME is not the default. Export it in your shell before running\n'
    printf '  tvbridge commands:  export TVBRIDGE_HOME="%s"\n' "$TVBRIDGE_HOME"
fi
printf '\n'
