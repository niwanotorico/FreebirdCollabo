#!/bin/sh
# Reproduce the VPS setup (relay + Caddy, relay/vps/docker-compose.yml) locally and run the
# sync tests through it over wss:// — new add-on, and new + v0.12.1 add-ons mixed.
#
#   sh tools/vps_docker_test.sh          (needs docker compose, python3 with bpy, openssl not needed)
#
# Caddy serves "localhost" with a certificate from its internal CA; the tests trust it through
# SSL_CERT_FILE.  Everything is removed again at the end (docker compose down -v).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT/relay/vps"
export RELAY_DOMAIN=localhost HTTP_PORT=${HTTP_PORT:-8080} HTTPS_PORT=${HTTPS_PORT:-8443}
TMP=$(mktemp -d)
cleanup() { docker compose down -v >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

docker compose up -d --build
for i in $(seq 1 30); do
  docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt "$TMP/root.crt" >/dev/null 2>&1 && break
  sleep 1
done
export SSL_CERT_FILE="$TMP/root.crt" COLLAB_RELAY_URL="wss://localhost:$HTTPS_PORT"
cd "$ROOT"
for i in $(seq 1 30); do python3 relay/relay_check.py "$COLLAB_RELAY_URL" && break; sleep 1; done

echo "== /stats"; python3 -c "import urllib.request,os;print(urllib.request.urlopen('https://localhost:'+os.environ['HTTPS_PORT']+'/stats').read().decode())"
echo "== test_sync.py wss (new add-on x2)"; python3 tests/test_sync.py wss
echo "== test_relay_traffic.py unit (wire protocol, batch)"; python3 tests/test_relay_traffic.py unit
echo "== test_relay_traffic.py live (new + new + v0.12.1)"; python3 tests/test_relay_traffic.py live
echo "== relay log"; (cd relay/vps && docker compose logs relay | tail -5)
echo "VPS DOCKER PASS"
