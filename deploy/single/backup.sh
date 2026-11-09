#!/usr/bin/env bash
# Backup of Lumen's data while it runs (SQLite online backup): accounts (control.db), every organisation's
# database and the demo template. Demo visitors' copies are skipped. Kept 14 days in /var/backups/lumen.
# install.sh runs it every day at 03:17. By hand:  sudo bash deploy/single/backup.sh
#
# Restore one:  sudo bash deploy/single/restore.sh /var/backups/lumen/lumen-YYYYMMDD-HHMM.tar.gz
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
DEST=/var/backups/lumen
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p "$DEST" && chmod 700 "$DEST"
docker compose exec -T lumen python - <<'PY'
import glob, os, shutil, sqlite3
src, out = "/data", "/data/.backup"
shutil.rmtree(out, ignore_errors=True)
files = [os.path.join(src, f) for f in ("control.db", "lumen.db")] + sorted(glob.glob(os.path.join(src, "tenants", "*.db")))
for f in files:
    if not os.path.exists(f):
        continue
    dst = os.path.join(out, os.path.relpath(f, src))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    s, d = sqlite3.connect(f), sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()
for f in ("secret.key",):
    if os.path.exists(os.path.join(src, f)):
        shutil.copy2(os.path.join(src, f), os.path.join(out, f))
print(f"{len(files)} databases copied")
PY
docker compose cp lumen:/data/.backup "$DEST/lumen-$STAMP" >/dev/null
docker compose exec -T lumen rm -rf /data/.backup
tar -C "$DEST" -czf "$DEST/lumen-$STAMP.tar.gz" "lumen-$STAMP" && rm -rf "${DEST:?}/lumen-$STAMP"
chmod 600 "$DEST/lumen-$STAMP.tar.gz"
find "$DEST" -name 'lumen-*.tar.gz' -mtime +14 -delete
echo "$(date -Is) backup: $DEST/lumen-$STAMP.tar.gz"
