#!/bin/bash
# LogMon Installer
# VGX Consulting - Log Monitoring Solution
# Copyright (c) 2026 VGX Consulting. All rights reserved.
#
# Usage:
#   curl -sfL https://raw.githubusercontent.com/VGXDigital/log-mon/main/install.sh | bash
#   curl -sfL https://raw.githubusercontent.com/VGXDigital/log-mon/main/install.sh | bash -s -- /opt/logmon

set -euo pipefail

REPO="VGXDigital/log-mon"
INSTALL_DIR="${1:-$HOME}"

log() { echo "[LogMon] $*"; }

log "Fetching latest release..."
RELEASE_JSON=$(curl -sfL "https://api.github.com/repos/$REPO/releases/latest")
TAG=$(echo "$RELEASE_JSON" | grep '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')
VERSION="${TAG#v}"

if [ -z "$VERSION" ]; then
    log "ERROR: Could not determine latest version"
    exit 1
fi

log "Installing LogMon v$VERSION to $INSTALL_DIR"

# Download and extract
TARBALL="log_monitor-v${VERSION}-linux-x86_64.tar.gz"
URL="https://github.com/$REPO/releases/download/$TAG/$TARBALL"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

curl -sfL -o "$TMP_DIR/$TARBALL" "$URL"
tar -xzf "$TMP_DIR/$TARBALL" -C "$TMP_DIR"

# Install
mkdir -p "$INSTALL_DIR"
mv "$TMP_DIR/log_monitor" "$INSTALL_DIR/log_monitor"
chmod +x "$INSTALL_DIR/log_monitor"

# Create config from example if none exists
if [ ! -f "$INSTALL_DIR/log_monitor.conf" ] && [ -f "$TMP_DIR/log_monitor.conf.example" ]; then
    mv "$TMP_DIR/log_monitor.conf.example" "$INSTALL_DIR/log_monitor.conf.example"
    log "Example config saved to $INSTALL_DIR/log_monitor.conf.example"
fi

log "Installed: $INSTALL_DIR/log_monitor"
"$INSTALL_DIR/log_monitor" --version

cat <<EOF

Setup:
  1. cp $INSTALL_DIR/log_monitor.conf.example $INSTALL_DIR/log_monitor.conf
  2. Edit log_monitor.conf with your SMTP and path settings
  3. Test: cd $INSTALL_DIR && ./log_monitor --debug
  4. Add to cron:
     crontab -e
     */15 * * * * cd $INSTALL_DIR && ./log_monitor

LogMon auto-updates itself — no manual updates needed.
EOF
