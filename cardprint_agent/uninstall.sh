#!/bin/bash
# SPDX-License-Identifier: MIT
# Stop and remove the card print LaunchAgent. Leaves the config, the log and the
# Keychain item (remove that with: security delete-generic-password -s cardprint -a erpnext).
set -euo pipefail
LABEL="farm.fafo.cardprint"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -f "$HOME/Library/Application Support/cardprint/cardprint_agent.py"
echo "The card print agent is stopped and removed. Queued cards stay queued in ERPNext."
