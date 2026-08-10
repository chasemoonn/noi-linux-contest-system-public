#!/bin/bash
set -euo pipefail

pm2="${PM2_BIN:-/nix/store/6v055r3d3s1r6wdjs78ip1nzzhy8q1xz-pm2-5.4.2/lib/node_modules/pm2/bin/pm2}"
mongod="${MONGOD_BIN:-/root/.nix-profile/bin/mongod}"
mongo_cache_gb="${MONGO_CACHE_GB:-1.23}"
backup="/root/.pm2/dump.pm2.noi-backup.$(date -u +%Y%m%dT%H%M%SZ)"

cp -a /root/.pm2/dump.pm2 "${backup}"

rollback() {
  echo 'MongoDB hardening failed; restoring public bind for service recovery' >&2
  "${pm2}" delete mongodb >/dev/null 2>&1 || true
  "${pm2}" start "${mongod}" --name mongodb -- \
    --auth --bind_ip 0.0.0.0 --wiredTigerCacheSizeGB="${mongo_cache_gb}" >/dev/null || true
  "${pm2}" save --force >/dev/null || true
}
trap rollback ERR

"${pm2}" delete mongodb >/dev/null
"${pm2}" start "${mongod}" --name mongodb -- \
  --auth --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB="${mongo_cache_gb}" >/dev/null

for _ in $(seq 1 30); do
  if ss -ltn '( sport = :27017 )' | grep -q '127.0.0.1:27017'; then
    break
  fi
  sleep 1
done
ss -ltn '( sport = :27017 )' | grep -q '127.0.0.1:27017'
if ss -ltn '( sport = :27017 )' | grep -Eq '0\.0\.0\.0:27017|\*:27017|\[::\]:27017'; then
  echo 'MongoDB still has a wildcard listener' >&2
  exit 1
fi

for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8888/ >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://127.0.0.1:8888/ >/dev/null
curl -fsS http://127.0.0.1:8600/healthz >/dev/null
"${pm2}" save --force >/dev/null

trap - ERR
echo "mongo_loopback_only backup=${backup}"
