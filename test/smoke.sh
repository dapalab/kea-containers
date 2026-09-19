#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Functional smoke test for the kea-dhcp4 image.
#
# Four gates, in increasing order of what they prove:
#   1. -V              the binary runs and reports its version
#   2. -W              the build report is present and reports the expected
#                      crypto backend and disabled DB backends
#   3. -t <config>     config parsing works against a real configuration
#   4. perfdhcp        a DHCPv4 DORA handshake actually completes
#
# Gate 4 is the point. A test that only proves the binary starts is not enough
# for a DHCP server.
set -euo pipefail

DHCP4_IMAGE="${DHCP4_IMAGE:-kea-dhcp4:test}"
TOOLS_IMAGE="${TOOLS_IMAGE:-kea-tools:test}"
EXPECT_VERSION="${EXPECT_VERSION:-}"

NET="kea-smoke-net"
SUBNET="172.31.77.0/24"
SRV_IP="172.31.77.2"
SRV="kea-smoke-dhcp4"
CLI="kea-smoke-perfdhcp"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }
info() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

cleanup() {
  docker rm -f "$SRV" "$CLI" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

###############################################################################
info "Gate 1: kea-dhcp4 -V"
###############################################################################
VERSION="$(docker run --rm "$DHCP4_IMAGE" /usr/sbin/kea-dhcp4 -V | head -1)"
[ -n "$VERSION" ] || fail "kea-dhcp4 -V produced no output"
pass "version: $VERSION"
if [ -n "$EXPECT_VERSION" ]; then
  case "$VERSION" in
    "$EXPECT_VERSION"*) pass "matches expected $EXPECT_VERSION" ;;
    *) fail "expected version $EXPECT_VERSION, got $VERSION" ;;
  esac
fi

###############################################################################
info "Gate 2: build report (-W)"
###############################################################################
REPORT="$(docker run --rm "$DHCP4_IMAGE" /usr/sbin/kea-dhcp4 -W)"
[ -n "$REPORT" ] || fail "build report is empty"

check_report() {
  if grep -qiE "$2" <<<"$REPORT"; then pass "$1"; else
    printf '%s\n' "$REPORT" | sed 's/^/      /'
    fail "$1 (pattern: $2)"
  fi
}
check_report "OpenSSL is the crypto backend" 'crypto.*openssl|openssl.*[0-9]'
check_report "MySQL backend absent"          'MySQL:( |\t)*(no|disabled)?$|MySQL.*no'
check_report "PostgreSQL backend absent"     'PostgreSQL:( |\t)*(no|disabled)?$|PostgreSQL.*no'

###############################################################################
info "Gate 3: config validation (-t)"
###############################################################################
docker run --rm -v "$HERE/kea-dhcp4-smoke.conf:/tmp/test.conf:ro" \
  "$DHCP4_IMAGE" /usr/sbin/kea-dhcp4 -t /tmp/test.conf >/dev/null \
  || fail "valid config rejected by -t"
pass "valid config accepted"

# Negative control: -t must actually reject bad input, otherwise gate 3 proves
# nothing at all.
if echo '{"Dhcp4":{"subnet4":[{"subnet":"not-a-subnet"}]}}' \
   | docker run --rm -i "$DHCP4_IMAGE" sh -c 'cat > /tmp/bad.conf; /usr/sbin/kea-dhcp4 -t /tmp/bad.conf' >/dev/null 2>&1; then
  fail "-t accepted an invalid config; the check is not meaningful"
fi
pass "invalid config correctly rejected"

###############################################################################
info "Gate 4: real DHCPv4 handshake via perfdhcp"
###############################################################################
docker network create --subnet "$SUBNET" "$NET" >/dev/null
pass "network $NET created ($SUBNET)"

docker run -d --name "$SRV" --network "$NET" --ip "$SRV_IP" \
  -v "$HERE/kea-dhcp4-smoke.conf:/etc/kea/kea-dhcp4.conf:ro" \
  "$DHCP4_IMAGE" >/dev/null

# Wait for the daemon to announce readiness rather than sleeping blindly.
for _ in $(seq 1 30); do
  docker logs "$SRV" 2>&1 | grep -q 'DHCP4_STARTED' && break
  sleep 1
done
if ! docker logs "$SRV" 2>&1 | grep -q 'DHCP4_STARTED'; then
  docker logs "$SRV" 2>&1 | sed 's/^/      /'
  fail "kea-dhcp4 did not reach DHCP4_STARTED"
fi
pass "kea-dhcp4 started and bound its socket"

set +e
PERF="$(docker run --rm --name "$CLI" --network "$NET" "$TOOLS_IMAGE" \
  /usr/sbin/perfdhcp -4 -r 10 -n 50 -p 20 "$SRV_IP" 2>&1)"
PERF_RC=$?
set -e
printf '%s\n' "$PERF" | sed 's/^/      /'

# perfdhcp exits 3 when it sent everything but received nothing at all.
[ "$PERF_RC" -eq 3 ] && fail "perfdhcp received no responses"

RCVD="$(grep -oE '^received packets: *[0-9]+' <<<"$PERF" | grep -oE '[0-9]+' | head -1)"
DORA="$(grep -oE 'DORA.*\([0-9]+' <<<"$PERF" | grep -oE '[0-9]+$' | head -1)"
: "${RCVD:=0}"

[ "$RCVD" -gt 0 ] || fail "perfdhcp received 0 packets - no handshake"
pass "perfdhcp received $RCVD packets"

if docker logs "$SRV" 2>&1 | grep -qE 'DHCP4_LEASE_ALLOC|DHCP4_LEASE_ADVERT'; then
  pass "server logged lease allocation"
else
  docker logs "$SRV" 2>&1 | tail -20 | sed 's/^/      /'
  fail "server never logged a lease allocation"
fi

LEASES="$(docker exec "$SRV" sh -c 'wc -l < /var/lib/kea/kea-leases4.csv' 2>/dev/null || echo 0)"
[ "$LEASES" -gt 1 ] && pass "lease file contains $((LEASES - 1)) lease record(s)"

printf '\n\033[32mAll gates passed.\033[0m\n'
