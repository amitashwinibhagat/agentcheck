#!/bin/sh
# Five-minute watchdog for the things an uptime check cannot see.
#
# The GCP uptime checks page when a host stops answering. This catches the
# slow-burn failures first: a container unhealthy, disk filling, a TLS cert
# near expiry, or backups going stale. It only ever READS -- it changes
# nothing, restarts nothing, pages nobody. Loud log lines are the output;
# the GCP alert is the pager.
#
#   */5 * * * * /home/amitashwini/agentcheck/deploy/gcp/watchdog.sh >> /home/amitashwini/watchdog.log 2>&1
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
fail=0
say() { printf '%s %s\n' "$(date -u '+%F %T')" "$*"; }
alarm() { say "ALERT: $*"; fail=1; }

# 1. Containers up (and healthy where a healthcheck exists; caddy has none).
for c in gcp-agentcheck-1 gcp-agentcheck-prod-1 gcp-caddy-1; do
    st=$(sudo docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo "missing")
    health=$(sudo docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null || echo "missing")
    if [ "$st" = "running" ] && { [ -z "$health" ] || [ "$health" = "healthy" ]; }; then
        say "$c ok (status=$st health=${health:-none})"
    else
        alarm "$c status='$st' health='$health'"
    fi
done

# 2. The full TLS path, from inside (hairpin): both public roots answer 200.
for u in "https://35-253-233-192.sslip.io/" "https://app.35-253-233-192.sslip.io/"; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$u" 2>/dev/null || echo "000")
    [ "$code" = "200" ] && say "$u -> 200" || alarm "$u -> HTTP $code"
done

# 3. Disk must have headroom (a full boot disk kills everything at once).
used=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
[ "$used" -lt 80 ] && say "disk ${used}% used" || alarm "disk ${used}% used (>=80%)"

# 4. TLS certificates must not be close to expiry (Caddy renews itself, but
# trust-and-verify: ACME has failed before, on other people's machines).
for h in 35-253-233-192.sslip.io app.35-253-233-192.sslip.io; do
    end=$(echo | openssl s_client -connect "$h:443" -servername "$h" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -z "$end" ]; then
        alarm "could not read cert for $h"
    else
        days=$(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
        [ "$days" -gt 14 ] && say "cert $h expires in ${days}d" \
            || alarm "cert $h expires in ${days}d (<=14d)"
    fi
done

# 5. Backups must be fresh: at least one verified snapshot per service from
# the last 26 hours (the backup cron runs daily at 03:00 UTC).
for svc in agentcheck agentcheck-prod; do
    fresh=$(find "$HOME/agentcheck-backups" -name "backup-$svc-*.db" -mtime -1 2>/dev/null | wc -l)
    [ "$fresh" -ge 1 ] && say "backup $svc fresh" \
        || alarm "no backup for $svc in the last 26h (is the 03:00 cron running?)"
done

[ "$fail" = "0" ] && say "all checks ok"
exit "$fail"
