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
#
# Logs are captured to a variable before matching. Piping `docker logs` into
# `grep -q` looks natural but is wrong under `set -o pipefail`: grep exits at
# the first match, docker logs takes SIGPIPE, and the pipeline reports failure
# even though the match succeeded.
for _ in $(seq 1 30); do
  LOGS="$(docker logs "$SRV" 2>&1 || true)"
  case "$LOGS" in *DHCP4_STARTED*) break ;; esac
  sleep 1
done
case "$LOGS" in
  *DHCP4_STARTED*) ;;
  *) printf '%s\n' "$LOGS" | sed 's/^/      /'
     fail "kea-dhcp4 did not reach DHCP4_STARTED" ;;
esac
pass "kea-dhcp4 started and bound its socket"

set +e
PERF="$(docker run --rm --name "$CLI" --network "$NET" "$TOOLS_IMAGE" \
  /usr/sbin/perfdhcp -4 -r 10 -n 50 -p 20 "$SRV_IP" 2>&1)"
PERF_RC=$?
set -e
printf '%s\n' "$PERF" | sed 's/^/      /'

# perfdhcp's exit code is not a pass/fail signal for us: it returns 3 when any
# packet was dropped, which a single warm-up drop triggers. Parse the
# section-scoped statistics instead and assert on the exchange that matters.
#
# DISCOVER-OFFER proves the server answers. REQUEST-ACK proves the full
# four-way handshake completed and a lease was actually committed.
eval "$(printf '%s\n' "$PERF" | python3 -c '
import re, sys
text = sys.stdin.read()
out = {}
for name, body in re.findall(r"\*\*\*Statistics for: (\S+)\*\*\*(.*?)(?=\*\*\*|\Z)", text, re.S):
    sent = re.search(r"^\s*sent packets:\s*(\d+)", body, re.M)
    rcvd = re.search(r"^\s*received packets:\s*(\d+)", body, re.M)
    key = name.replace("-", "_")
    out[f"{key}_SENT"] = sent.group(1) if sent else "0"
    out[f"{key}_RCVD"] = rcvd.group(1) if rcvd else "0"
for k, v in out.items():
    print(f"{k}={v}")
')"

: "${DISCOVER_OFFER_RCVD:=0}" "${REQUEST_ACK_SENT:=0}" "${REQUEST_ACK_RCVD:=0}"

[ "$DISCOVER_OFFER_RCVD" -gt 0 ] || fail "server sent no DHCPOFFER"
pass "DISCOVER-OFFER: $DISCOVER_OFFER_RCVD received"

[ "$REQUEST_ACK_RCVD" -gt 0 ] || fail "no DHCPACK - handshake never completed"

# Allow a small warm-up loss rather than demanding a perfect run: the first
# DISCOVER can be sent before the server has finished binding. Anything below
# 90% means something is genuinely wrong.
if [ "$REQUEST_ACK_SENT" -gt 0 ]; then
  PCT=$(( REQUEST_ACK_RCVD * 100 / REQUEST_ACK_SENT ))
  [ "$PCT" -ge 90 ] || fail "only ${PCT}% of REQUESTs were ACKed (${REQUEST_ACK_RCVD}/${REQUEST_ACK_SENT})"
  pass "REQUEST-ACK: ${REQUEST_ACK_RCVD}/${REQUEST_ACK_SENT} (${PCT}%) - full DORA handshake completed"
fi

if [ "$PERF_RC" -ne 0 ]; then
  printf '  \033[33mnote\033[0m  perfdhcp exited %d (drops occurred); assertions above are authoritative\n' "$PERF_RC"
fi

LOGS="$(docker logs "$SRV" 2>&1 || true)"
case "$LOGS" in
  *DHCP4_LEASE_ALLOC*|*DHCP4_LEASE_OFFER*)
    pass "server logged lease allocation" ;;
  *)
    printf '%s\n' "$LOGS" | tail -20 | sed 's/^/      /'
    fail "server never logged a lease allocation" ;;
esac

LEASES="$(docker exec "$SRV" sh -c 'wc -l < /var/lib/kea/kea-leases4.csv' 2>/dev/null || echo 0)"
[ "$LEASES" -gt 1 ] && pass "lease file contains $((LEASES - 1)) lease record(s)"

printf '\n\033[32mAll gates passed.\033[0m\n'
