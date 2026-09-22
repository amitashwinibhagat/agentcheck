#!/bin/sh
# Nightly SQLite backups for every app instance in this compose project.
#
# Uses the sqlite .backup API (safe against a running writer -- unlike cp),
# copies the snapshot to the host, verifies it opens with integrity ok, and
# prunes to retention. Run from cron; everything important goes to stdout so
# the cron log is the audit trail. Exit nonzero on any failure so a silent
# half-backup is impossible.
#
#   0 3 * * * /home/amitashwini/agentcheck/deploy/gcp/backup.sh >> /home/amitashwini/agentcheck-backups/backup.log 2>&1
#
# The watchdog (see docs) checks that a fresh backup exists; this script
# deliberately does not page anyone itself.
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
STAMP=$(date -u +%F)
OUT="$HOME/agentcheck-backups"
KEEP=7
mkdir -p "$OUT"

log() { printf '%s %s\n' "$(date -u '+%F %T')" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

command -v python3 >/dev/null || fail "host python3 missing (needed to verify backups)"

for svc in agentcheck agentcheck-prod; do
    tmp="backup-$svc-$STAMP.db"
    log "$svc: snapshotting live DB via sqlite .backup API"
    sudo docker compose exec -T "$svc" python -c "
import sqlite3, os
src = os.environ['AGENTCHECK_HOME'] + '/agentcheck.db'
sqlite3.connect(src).backup(sqlite3.connect('/tmp/$tmp'))
print('snapshot ok')" || fail "$svc: snapshot failed"
    sudo docker compose cp "$svc:/tmp/$tmp" "$OUT/$tmp" || fail "$svc: copy to host failed"
    sudo chown "$USER" "$OUT/$tmp"  # cp runs as root; retention runs as us
    sudo docker compose exec -T "$svc" rm -f "/tmp/$tmp"
    # Verify the artifact itself: opens, integrity ok, has keys and results.
    check=$(python3 - "$OUT/$tmp" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("integrity_check failed")
keys = c.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
res = c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
print(f"integrity ok, keys={keys} results={res}")
EOF
) || fail "$svc: verification failed ($check)"
    log "$svc: $check"
    # Retention: newest $KEEP per service survive.
    # shellcheck disable=SC2012
    ls -t "$OUT"/backup-"$svc"-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
    log "$svc: retained $(ls "$OUT"/backup-"$svc"-*.db 2>/dev/null | wc -l) snapshots"
done
log "done"
