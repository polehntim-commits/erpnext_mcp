#!/bin/bash
# SPDX-License-Identifier: MIT
# Install the card print agent on this Mac. docs/design/card_print_queue.md §6.
#
#   ./install.sh            interactive: config, Keychain, LaunchAgent
#
# It reads the printer's real option names from the driver, writes
# ~/.config/cardprint/config.toml, stores the ERPNext API key in the Keychain,
# and loads a LaunchAgent that starts at login and restarts if it exits.
# IT NEVER PRINTS WITHOUT ASKING: the test card is a yes/no prompt, default no.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="farm.fafo.cardprint"
APP_DIR="$HOME/Library/Application Support/cardprint"
CONFIG_DIR="$HOME/.config/cardprint"
CONFIG="$CONFIG_DIR/config.toml"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/cardprint.log"

say() { printf '%s\n' "$*"; }
ask() { local answer; read -r -p "$1 [$2]: " answer; printf '%s' "${answer:-$2}"; }

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { say "python3 was not found. Install the Xcode command line tools (xcode-select --install)."; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { say "python3 must be 3.9 or newer."; exit 1; }

say "== Printer"
DEFAULT_QUEUE="$(lpstat -p 2>/dev/null | awk '/^printer/ {print $2}' | grep -i -m1 'primacy' || true)"
[ -n "$DEFAULT_QUEUE" ] || DEFAULT_QUEUE="$(lpstat -p 2>/dev/null | awk '/^printer/ {print $2; exit}')"
[ -n "$DEFAULT_QUEUE" ] || { say "No printer is set up in macOS. Add the Evolis Primacy 2 in Printers & Scanners first."; exit 1; }
QUEUE="$(ask "CUPS queue" "$DEFAULT_QUEUE")"
OPTIONS="$(lpoptions -p "$QUEUE" -l 2>/dev/null || true)"
[ -n "$OPTIONS" ] || { say "lpoptions has nothing for '$QUEUE'."; exit 1; }

# Option names are READ from the driver, never guessed.
MEDIA="PageSize=$(printf '%s\n' "$OPTIONS" | awk -F': ' '/^PageSize\// {print $2}' | tr ' ' '\n' | sed -n 's/^\*//p' | head -1)"
DUPLEX_VALUES="$(printf '%s\n' "$OPTIONS" | awk -F': ' '/^Duplex\// {print $2}')"
DUPLEX_ON="$(printf '%s\n' "$DUPLEX_VALUES" | tr ' ' '\n' | sed 's/^\*//' | grep -i -m1 'NoTumble' || true)"
DUPLEX_OFF="$(printf '%s\n' "$DUPLEX_VALUES" | tr ' ' '\n' | sed 's/^\*//' | grep -i -m1 -E '^(NONE|None|Off)$' || true)"
say "  page size option : $MEDIA"
say "  single-sided     : Duplex=${DUPLEX_OFF:-(not offered)}"
say "  dual-sided       : Duplex=${DUPLEX_ON:-(not offered)}"

say "== ERPNext"
URL="$(ask "ERPNext URL (reachable from this Mac)" "http://umbrel.local:5300")"
STATION="$(ask "Print station name (the Card Print Station record)" "primacy2-main")"

mkdir -p "$CONFIG_DIR" "$APP_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
if [ -f "$CONFIG" ]; then
	cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
	say "  (the existing config was backed up)"
fi
cat > "$CONFIG" <<TOML
# Written by install.sh on $(date '+%Y-%m-%d %H:%M'). Option names were read from the driver.
erpnext_url    = "$URL"
station        = "$STATION"
queue          = "$QUEUE"
media_option   = "$MEDIA"
single_option  = "${DUPLEX_OFF:+Duplex=$DUPLEX_OFF}"
duplex_option  = "${DUPLEX_ON:+Duplex=$DUPLEX_ON}"
extra_options  = ["fit-to-page"]
poll_seconds          = 5
backoff_seconds       = 30
print_timeout_seconds = 180
keychain_service = "cardprint"
keychain_account = "erpnext"
TOML
chmod 600 "$CONFIG"
cp "$HERE/cardprint_agent.py" "$APP_DIR/cardprint_agent.py"
say "  wrote $CONFIG"

say "== Credentials (the agent's own ERPNext user, role: Card Print Station)"
if security find-generic-password -s cardprint -a erpnext >/dev/null 2>&1; then
	REUSE="$(ask "An API key is already in the Keychain. Keep it?" "y")"
else
	REUSE="n"
fi
if [ "$REUSE" != "y" ]; then
	read -r -p "API key: " API_KEY
	read -r -s -p "API secret (not shown): " API_SECRET; echo
	security add-generic-password -U -s cardprint -a erpnext -w "$API_KEY:$API_SECRET" >/dev/null
	unset API_SECRET
	say "  stored in the login Keychain (service 'cardprint')"
fi

say "== LaunchAgent"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$PYTHON</string>
		<string>$APP_DIR/cardprint_agent.py</string>
	</array>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ThrottleInterval</key><integer>10</integer>
	<key>ProcessType</key><string>Background</string>
	<key>StandardErrorPath</key><string>$HOME/Library/Logs/cardprint.stderr.log</string>
	<key>StandardOutPath</key><string>/dev/null</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
say "  loaded $LABEL (starts at login, restarts if it exits)"

say "== Check"
"$PYTHON" "$APP_DIR/cardprint_agent.py" --status || true

TEST="$(ask "Print one TEST card now? It uses one card and ribbon panel" "n")"
if [ "$TEST" = "y" ]; then
	"$PYTHON" "$APP_DIR/cardprint_agent.py" --test-card || say "  the test card did not print — see $LOG"
	say "  If the card is sideways or small, add \"Orientation=LANDSCAPE_CC90\" to extra_options in $CONFIG and run:"
	say "    launchctl kickstart -k gui/$(id -u)/$LABEL"
else
	say "  no test card printed. Later: python3 \"$APP_DIR/cardprint_agent.py\" --test-card"
fi
say "Done. Log: $LOG"
