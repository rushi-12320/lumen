#!/usr/bin/env bash
# Put a backup made by backup.sh back in place. It replaces the accounts and every organisation's database that is
# in the backup, so run backup.sh first if the current data might still be needed.
#   sudo bash deploy/single/restore.sh /var/backups/lumen/lumen-YYYYMMDD-HHMM.tar.gz
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
FILE=${1:?"usage: sudo bash restore.sh /var/backups/lumen/lumen-YYYYMMDD-HHMM.tar.gz"}
[ -f "$FILE" ] || { echo "No such file: $FILE"; exit 1; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$FILE" -C "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
[ -n "$SRC" ] && [ -f "$SRC/control.db" ] || { echo "This is not a Lumen backup (no control.db inside)."; exit 1; }
if [ -t 0 ]; then
  read -rp "Replace Lumen's current data with $(basename "$FILE")? Type RESTORE to continue: " ok
  [ "$ok" = RESTORE ] || { echo "Nothing changed."; exit 1; }
fi
docker compose stop lumen
docker compose cp "$SRC/." lumen:/data/
# files copied into a container belong to root: give them back to the app's user, and drop stale write-ahead logs
docker compose run --rm --no-deps --user root --entrypoint sh lumen -c \
  'rm -f /data/*.db-wal /data/*.db-shm /data/tenants/*.db-wal /data/tenants/*.db-shm; chown -R 10001:10001 /data'
docker compose start lumen
echo "Restored from $FILE"
