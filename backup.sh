#!/usr/bin/env bash
set -euo pipefail

TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_NAME="cyborg_backup_${TIMESTAMP}.zip"
BACKUP_PATH="$HOME/${BACKUP_NAME}"
WORKSPACE_DIR="$HOME/.config/cyborg"
DATA_DIR="$HOME/.local/share/cyborg"

# Collect files into a temp staging dir
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# Databases
for db in \
  "$HOME/.local/share/cyborg/cyborg.db" \
  "$HOME/.config/cyborg/harness/cyborg.db" \
  "$HOME/.config/cyborg/cyborg.db" \
  "$HOME/.local/share/cyborg/whatsappbridge/.env" \
  "$HOME/.openclaw/lcm.db" \
  "$HOME/.openclaw/bobvoice-sessions.db"; do
  if [ -f "$db" ]; then
    dest="$STAGE/$(echo "$db" | sed "s|^$HOME/||")"
    mkdir -p "$(dirname "$dest")"
    cp "$db" "$dest"
  fi
done

# Workspace config and files
if [ -d "$WORKSPACE_DIR" ]; then
  mkdir -p "$STAGE/.config/cyborg"
  cp -r "$WORKSPACE_DIR"/.env "$STAGE/.config/cyborg/" 2>/dev/null || true
  for f in "$WORKSPACE_DIR"/settings*.json; do
    [ -f "$f" ] || continue
    dest="$STAGE/$(echo "$f" | sed "s|^$HOME/||")"
    mkdir -p "$(dirname "$dest")"
    cp "$f" "$dest"
  done
  # Full harness workspace (scripts, artifacts, generated-images, etc)
  if [ -d "$WORKSPACE_DIR/harness" ]; then
    rsync -a --exclude='*.log' "$WORKSPACE_DIR/harness/" "$STAGE/.config/cyborg/harness/" 2>/dev/null || \
      cp -r "$WORKSPACE_DIR/harness" "$STAGE/.config/cyborg/"
  fi
fi

# Data directory (non-code runtime data)
if [ -d "$DATA_DIR" ]; then
  mkdir -p "$STAGE/.local/share"
  cp -r "$DATA_DIR" "$STAGE/.local/share/" \
    --exclude='*.log' 2>/dev/null || \
    rsync -a --exclude='*.log' "$DATA_DIR/" "$STAGE/.local/share/cyborg/" 2>/dev/null || \
    cp -r "$DATA_DIR" "$STAGE/.local/share/"
fi

# Project-level config files (not source code)
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
for f in \
  "$PROJECT_DIR/.env" \
  "$PROJECT_DIR/.env.local" \
  "$PROJECT_DIR/pyproject.toml" \
  "$PROJECT_DIR/DOCS.yaml" \
  "$PROJECT_DIR/packages/cyborg-server/pyproject.toml"; do
  [ -f "$f" ] || continue
  dest="$STAGE/$(echo "$f" | sed "s|^$HOME/||")"
  mkdir -p "$(dirname "$dest")"
  cp "$f" "$dest"
done

# Zip it up
cd "$STAGE"
zip -r "$BACKUP_PATH" .
cd - > /dev/null

echo "Backup created: ${BACKUP_PATH}"
echo "Size: $(du -h "$BACKUP_PATH" | cut -f1)"
