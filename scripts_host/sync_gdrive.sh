#!/bin/bash
# sync_gdrive.sh — Package and sync the MPCUTER workspace to Google Drive ("MPCUTER v1.0.0 Backup")
set -e
WORKSPACE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GDRIVE_FOLDER_ID="1404SHTUb3yGlCDQPnYsc946P9dqwVhkE"
ARCHIVE_FILE_ID="1dr-yvKUnh4oRgHH3zNf6UFxEzWX7gfWX"
TMP_ARCHIVE="/tmp/mpcuter-antigravity-workspace-full.tar.gz"

echo "[MPCUTER Cloud Sync] Packaging workspace snapshot..."
cd "$(dirname "$WORKSPACE_DIR")"
tar \
  --exclude='sdk/.sdk-tools/zigenv' \
  --exclude='.venv-audio' \
  --exclude='.flow_secrets.json' \
  --exclude='.pio' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='mpc_control/webhook_inbox' \
  --exclude='*.log' \
  --exclude='*.pid' \
  --exclude='.webhook_server.pid' \
  -czf "$TMP_ARCHIVE" "$(basename "$WORKSPACE_DIR")"

echo "[MPCUTER Cloud Sync] Updating mpcuter-antigravity-workspace-full.tar.gz in Google Drive..."
gdrive mutate update "$ARCHIVE_FILE_ID" "$TMP_ARCHIVE"

echo "[OK] Workspace synced to Google Drive folder: https://drive.google.com/drive/folders/$GDRIVE_FOLDER_ID"
