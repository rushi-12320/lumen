#!/bin/sh
# Consistent backup of Lumen's database (safe while it runs). Keep 14 days.
#   crontab:  0 2 * * *  /opt/lumen/deploy/backup.sh
set -eu
cd "$(dirname "$0")"
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M)
docker compose exec -T lumen python -c "import sqlite3; s=sqlite3.connect('/data/lumen.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
docker compose cp lumen:/data/backup.db "backups/lumen-$STAMP.db"
docker compose exec -T lumen rm -f /data/backup.db
find backups -name 'lumen-*.db' -mtime +14 -delete
echo "backup: backups/lumen-$STAMP.db"
