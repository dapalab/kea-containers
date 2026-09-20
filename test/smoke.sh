#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Functional smoke test for the kea-dhcp4 image.
#
# Six gates, in increasing order of what they prove:
#   1. -V              the binary runs and reports its version
#   2. -W              the build report is present and reports the expected
#                      crypto backend and disabled DB backends
#   3. -t <config>     config parsing works against a real configuration
#   4. perfdhcp        a DHCPv4 DORA handshake actually completes
#   5. every image     each published image starts and reports healthy
#   6. kea-lfc         lease file cleanup actually compacts the lease file
#
# Gate 4 is the point. A test that only proves the binary starts is not enough
# for a DHCP server.
set -euo pipefail

DHCP4_IMAGE="${DHCP4_IMAGE:-kea-dhcp4:test}"
TOOLS_IMAGE="${TOOLS_IMAGE:-kea-tools:test}"
# Gate 5 checks the other published images too. Derived from DHCP4_IMAGE so a
# single variable still drives everything, local :test tags or published ones.
IMAGE_PREFIX="${IMAGE_PREFIX:-$(printf '%s' "$DHCP4_IMAGE" | sed 's/dhcp4:.*$//')}"
IMAGE_SUFFIX="${IMAGE_SUFFIX:-:$(printf '%s' "$DHCP4_IMAGE" | sed 's/^.*://')}"
EXPECT_VERSION="${EXPECT_VERSION:-}"

NET="kea-smoke-net"
SUBNET="172.31.77.0/24"
SRV_IP="172.31.77.2"
SRV="kea-smoke-dhcp4"
CLI="kea-smoke-perfdhcp"
LFC_SRV="kea-smoke-lfc"
LFC_BAD="kea-smoke-lfc-broken"
LFC_BAD_IMAGE="kea-smoke-lfc-broken:test"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Every image must carry the SPDX document describing Kea itself. Without it
# the published SBOM catalogues 25 apk packages and omits the DHCP server -
# an inventory that reads as diligence while missing the primary artifact.
# See build/make-sbom.sh and docs/DECISIONS.md D23.
check_sbom_doc() {  # check_sbom_doc <image-ref> <label>
  local doc err
  doc="$(docker run --rm --entrypoint cat "$1" /usr/share/sbom/kea.spdx.json 2>/dev/null)" \
    || fail "$2: /usr/share/sbom/kea.spdx.json is missing from the image"
  # stderr is captured rather than discarded: an earlier version of this
  # check sent it to /dev/null and hid a syntax error in its own validator,
  # so the assertion failed for a reason nobody could see.
  if ! err="$(printf '%s' "$doc" | python3 "$HERE/check-sbom-doc.py" "${EXPECT_VERSION:-}" 2>&1)"; then
    printf '%s\n' "$err" | sed 's/^/      /'
    fail "$2: the shipped SBOM document is invalid or disagrees with the image"
  fi
  pass "$2 ships a valid SBOM document naming Kea ${EXPECT_VERSION:-} ($err)"
}

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }
info() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

cleanup() {
  docker rm -f "$SRV" "$CLI" "$LFC_SRV" "$LFC_BAD" >/dev/null 2>&1 || true
  docker rmi -f "$LFC_BAD_IMAGE" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  [ -n "${LFC_CONF:-}" ] && rm -f "$LFC_CONF"
  return 0
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
check_report "OpenSSL is the crypto backend" 'OpenSSL:.*[0-9]'

# Kea is compiled from source, so buildx's SBOM scanner cannot see it; the
# image ships an SPDX document that puts it in the attestation. Checked here,
# before publishing, because the scanner silently skips a document it cannot
# parse and the SBOM would quietly go back to omitting Kea.
if [ -n "$EXPECT_VERSION" ]; then
  check_sbom_doc "$DHCP4_IMAGE" "dhcp4"
else
  printf '  \033[33mskip\033[0m  SBOM document check needs EXPECT_VERSION\n'
fi

# The DB backends are compiled in. The daemon links libmariadb and libpq
# directly - it never shells out to the mysql/psql CLI tools, which is why
# those clients are not in any image. See docs/DECISIONS.md D13.
check_report "MySQL backend compiled in"      'MySQL:[[:space:]]+[^n]'
check_report "PostgreSQL backend compiled in" 'PostgreSQL:[[:space:]]+[^n]'

# In Kea 3.x the DB backends are HOOK LIBRARIES, not built into the daemon.
# This is why `kea-dhcp4 -V` lists only memfile: it shows REGISTERED backends,
# and the DB ones register when their hook loads.
#
# Note what does NOT work as a test: `-t` with "type": "mysql" and no hook
# exits 0, because -t validates the backend NAME against a known list rather
# than its availability. (A bogus name like "notarealdb" does exit 1.) So an
# accepted config proves nothing about whether the backend was built.
#
# What does discriminate, verified against a real image:
#   - the hook library exists and links its client library
#   - loading it via -t succeeds, while a bogus hook path exits 1
for be in mysql pgsql; do
  case "$be" in
    mysql) type=mysql;      clientlib=libmariadb ;;
    pgsql) type=postgresql; clientlib=libpq ;;
  esac
  lib="/usr/lib/kea/hooks/libdhcp_${be}.so"

  docker run --rm "$DHCP4_IMAGE" test -f "$lib" \
    || fail "$lib missing - the $type backend was not built"

  docker run --rm "$DHCP4_IMAGE" sh -c "ldd $lib | grep -q $clientlib" \
    || fail "$lib does not link $clientlib"

  db_cfg() {
    printf '{"Dhcp4":{"interfaces-config":{"interfaces":[]},'\
'"hooks-libraries":[{"library":"%s"}],'\
'"lease-database":{"type":"%s","name":"kea","host":"db.invalid",'\
'"user":"u","password":"p"},"subnet4":[]}}' "$1" "$type"
  }
  run_t() {
    docker run --rm -i "$DHCP4_IMAGE" \
      sh -c 'cat > /tmp/db.conf; /usr/sbin/kea-dhcp4 -t /tmp/db.conf >/dev/null 2>&1; echo $?'
  }

  # Negative control first: a hook path that does not exist must fail, or the
  # positive check below would pass for the wrong reason.
  if [ "$(db_cfg /usr/lib/kea/hooks/libdhcp_definitely_not_here.so | run_t)" = "0" ]; then
    fail "a nonexistent hook path was accepted - the $type check proves nothing"
  fi

  [ "$(db_cfg "$lib" | run_t)" = "0" ] \
    || fail "$type backend config rejected with $lib loaded"

  pass "$type backend: hook present, links $clientlib, loads cleanly"
done

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

# -R 20 simulates 20 distinct clients. Without it perfdhcp reuses a single MAC,
# so the server answers every request with the SAME address (DHCP4_LEASE_REUSE
# in the logs) and the test proves only that one handshake works - not that the
# pool allocates. With it, we can assert distinct addresses were handed out.
set +e
PERF="$(docker run --rm --name "$CLI" --network "$NET" "$TOOLS_IMAGE" \
  /usr/sbin/perfdhcp -4 -R 20 -r 10 -n 50 -p 20 "$SRV_IP" 2>&1)"
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

# Count DISTINCT addresses in the memfile CSV, not rows: memfile appends a row
# per lease update, so a row count can look healthy while the server has been
# handing the same address to one client over and over.
CSV="$(docker exec "$SRV" cat /var/lib/kea/kea-leases4.csv 2>/dev/null || true)"
DISTINCT="$(printf '%s\n' "$CSV" | tail -n +2 | cut -d, -f1 | grep -cE '^172\.31\.77\.' || true)"
UNIQUE="$(printf '%s\n' "$CSV" | tail -n +2 | cut -d, -f1 | grep -E '^172\.31\.77\.' | sort -u | wc -l)"
: "${UNIQUE:=0}"

[ "$UNIQUE" -ge 5 ] \
  || fail "only $UNIQUE distinct address(es) allocated - the pool is not allocating"
pass "$UNIQUE distinct addresses allocated from the pool ($DISTINCT lease rows)"

# Every address must fall inside the configured pool, 172.31.77.100-200.
OUTSIDE="$(printf '%s\n' "$CSV" | tail -n +2 | cut -d, -f1 | grep -E '^172\.31\.77\.' \
           | awk -F. '$4 < 100 || $4 > 200' | head -3)"
if [ -n "$OUTSIDE" ]; then
  printf '      %s\n' "$OUTSIDE"
  fail "addresses allocated outside the configured pool"
fi
pass "all allocated addresses fall within the configured pool"

###############################################################################
info "Gate 5: every published image starts and reports healthy"
###############################################################################
# Gates 1-4 only exercise kea-dhcp4, with kea-tools as the perfdhcp driver.
# Without this gate a completely broken kea-dhcp6, kea-dhcp-ddns or
# kea-ctrl-agent would sail through CI and be published, because "it compiled"
# was the only thing being checked.
#
# Each image is started with the DEFAULT config it ships, which also proves
# those templates work as shipped rather than merely parsing.
check_starts() {  # $1=image suffix  $2=expected log token  $3="health" to also await healthy
  img="$1"; token="$2"; wants_health="$3"; name="kea-smoke-$1"; logs=""
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" "${IMAGE_PREFIX}${img}${IMAGE_SUFFIX}" >/dev/null 2>&1 \
    || fail "$img: container would not start at all"
  for _ in $(seq 1 30); do
    logs="$(docker logs "$name" 2>&1 || true)"
    case "$logs" in *"$token"*) break ;; esac
    sleep 1
  done
  case "$logs" in
    *"$token"*) pass "$img started ($token)" ;;
    *) printf '%s\n' "$logs" | tail -12 | sed 's/^/      /'
       docker rm -f "$name" >/dev/null 2>&1 || true
       fail "$img never logged $token" ;;
  esac

  if [ "$wants_health" = "health" ]; then
    st=""
    for _ in $(seq 1 24); do
      st="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo none)"
      [ "$st" = "healthy" ] && break
      [ "$st" = "none" ] && break
      sleep 5
    done
    if [ "$st" = "healthy" ]; then
      pass "$img healthcheck reports healthy"
    else
      docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$name" 2>/dev/null \
        | tail -5 | sed 's/^/      /'
      docker rm -f "$name" >/dev/null 2>&1 || true
      fail "$img healthcheck never became healthy (status: $st)"
    fi
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true

  [ -n "$EXPECT_VERSION" ] && check_sbom_doc "${IMAGE_PREFIX}${img}${IMAGE_SUFFIX}" "$img"
  return 0
}

check_starts dhcp6     DHCP6_STARTED     health
check_starts dhcp-ddns DHCP_DDNS_STARTED health

# ctrl-agent exists only on the 3.0 branch. Its healthcheck is an HTTP request
# rather than a unix socket probe, and this is the only place it is exercised
# against a real agent rather than a mock.
ca_img="${IMAGE_PREFIX}ctrl-agent${IMAGE_SUFFIX}"
if docker image inspect "$ca_img" >/dev/null 2>&1 || docker pull -q "$ca_img" >/dev/null 2>&1; then
  check_starts ctrl-agent CTRL_AGENT_STARTED health
else
  printf '  \033[33mskip\033[0m  kea-ctrl-agent not built for this branch\n'
fi

###############################################################################
info "Gate 6: kea-lfc actually compacts the lease file"
###############################################################################
# WHY THIS GATE EXISTS
#     kea-lfc ships in dhcp4, dhcp6 and tools, and both shipped templates
#     schedule it hourly - but nothing ever ran it. That is the same shape as
#     the kea-ctrl-agent bug: present, configured, plausible, never executed.
#
# WHAT ACTUALLY FAILS, VERIFIED
#     A MISSING kea-lfc is not the risk. kea-dhcp4 refuses to start without
#     it - "DHCPSRV_MEMFILE_FAILED_TO_OPEN ... File not found: /usr/sbin/
#     kea-lfc", DHCP4_INIT_FAIL - so gates 4 and 5 already cover that.
#
#     The real failure is a kea-lfc that is PRESENT and BROKEN: a missing
#     shared library, a wrong-architecture binary, a directory it cannot
#     write. Kea execs it and never checks the exit status, so:
#
#         container            : healthy
#         logs                 : LFC_START / LFC_EXECUTE, no error at all
#         lease file           : grows forever
#
#     Nothing surfaces until restart times degrade at week twelve.
#
# WHAT DISCRIMINATES, MEASURED
#     working kea-lfc : kea-leases4.csv.2 holds the COMPACTED lease set
#                       (one row per address), no .1 left behind
#     broken kea-lfc  : no .2 at all; an unprocessed .1 and a stale .pid
#
#     So the assertion is on .2 existing AND being compacted - it can only
#     get there by kea-lfc having run to completion.
# Removed by cleanup(); deliberately NOT given its own trap, which would
# replace the existing EXIT trap and leak the containers below.
LFC_CONF="$(mktemp)"
# mktemp creates 0600. The daemon runs as UID 10000, not as whoever runs this
# script, so a bind-mounted 0600 config is unreadable inside the container and
# Kea dies with "Unable to open file".
chmod 0644 "$LFC_CONF"
# Derived from the gate-4 config by sed rather than kept as a second file, so
# the two cannot drift apart.
sed 's|"persist": true|"persist": true,\n      "lfc-interval": 5|' \
  "$HERE/kea-dhcp4-smoke.conf" > "$LFC_CONF"
grep -q '"lfc-interval": 5' "$LFC_CONF" || fail "could not derive the LFC config"

# Sets LS_ROWS / LS_UNIQ / LS_LEFTOVER / LS_PID for one container.
# LS_ROWS is -1 when no compacted file exists at all.
LS_ROWS=-1; LS_UNIQ=-1; LS_LEFTOVER="?"; LS_PID="?"
read_lease_state() {
  local out
  out="$(docker exec "$1" sh -c '
    d=/var/lib/kea
    if [ -f "$d/kea-leases4.csv.2" ]; then
      rows=$(tail -n +2 "$d/kea-leases4.csv.2" | grep -c . || true)
      uniq=$(tail -n +2 "$d/kea-leases4.csv.2" | cut -d, -f1 | sort -u | grep -c . || true)
    else
      rows=-1; uniq=-1
    fi
    [ -f "$d/kea-leases4.csv.1" ] && one=yes || one=no
    [ -f "$d/kea-leases4.csv.pid" ] && pid=yes || pid=no
    echo "$rows $uniq $one $pid"' 2>/dev/null)" || out=""
  [ -n "$out" ] || out="-1 -1 ? ?"
  read -r LS_ROWS LS_UNIQ LS_LEFTOVER LS_PID <<<"$out"
}

run_lfc_server() {  # run_lfc_server <name> <ip> <image>
  docker run -d --name "$1" --network "$NET" --ip "$2" \
    -v "$LFC_CONF:/etc/kea/kea-dhcp4.conf:ro" "$3" >/dev/null
  for _ in $(seq 1 30); do
    case "$(docker logs "$1" 2>&1 || true)" in *DHCP4_STARTED*) return 0 ;; esac
    sleep 1
  done
  docker logs "$1" 2>&1 | tail -10 | sed 's/^/      /'
  return 1
}

# Wait for compaction rather than sleeping a fixed time: lfc-interval is 5s,
# so this normally settles in ~10s, and a slow runner does not flake it.
wait_for_compaction() {  # wait_for_compaction <container> <seconds>
  local container="$1" limit="$2" n=0
  while [ "$n" -lt "$limit" ]; do
    read_lease_state "$container"
    # Compacted means: a .2 exists, it holds the leases we drove in, and it
    # holds exactly one row per address. A file with duplicate rows is a
    # rotation that kea-lfc never finished processing.
    if [ "$LS_ROWS" -ge 5 ] && [ "$LS_ROWS" = "$LS_UNIQ" ]; then
      return 0
    fi
    n=$((n + 1)); sleep 1
  done
  return 1
}

docker rm -f "$SRV" >/dev/null 2>&1 || true   # free the gate-4 server's IP
run_lfc_server "$LFC_SRV" "$SRV_IP" "$DHCP4_IMAGE" \
  || fail "kea-dhcp4 did not start with lfc-interval set"
pass "kea-dhcp4 started with lfc-interval=5"

docker run --rm --name "$CLI" --network "$NET" "$TOOLS_IMAGE" \
  /usr/sbin/perfdhcp -4 -R 20 -r 10 -n 50 -p 15 "$SRV_IP" >/dev/null 2>&1 || true

if wait_for_compaction "$LFC_SRV" 60; then
  pass "kea-lfc compacted the lease file ($LS_ROWS rows, $LS_UNIQ distinct addresses)"
  [ "$LS_LEFTOVER" = "no" ] \
    || fail "kea-leases4.csv.1 was left behind - LFC did not finish"
else
  read_lease_state "$LFC_SRV"
  printf '      rows=%s distinct=%s leftover-.1=%s stale-pid=%s\n' \
    "$LS_ROWS" "$LS_UNIQ" "$LS_LEFTOVER" "$LS_PID"
  docker logs "$LFC_SRV" 2>&1 | grep -i lfc | tail -5 | sed 's/^/      /'
  fail "kea-lfc never produced a compacted lease file"
fi

# NEGATIVE CONTROL. Without this the assertion above is just another green
# tick: it has to be shown to go red when kea-lfc is broken, which is the
# failure mode it exists for. kea-lfc is replaced by a stub that exits 1 -
# present and executable, so Kea starts happily and reports healthy.
# Empty build context on purpose: the Dockerfile has no COPY, and the repo
# root would ship .git and everything else to the daemon for nothing.
LFC_CTX="$(mktemp -d)"
printf 'FROM %s\nUSER root\nRUN printf "#!/bin/sh\\nexit 1\\n" > /usr/sbin/kea-lfc \\\n && chmod 0755 /usr/sbin/kea-lfc\nUSER 10000:10000\n' \
  "$DHCP4_IMAGE" | docker build -q -t "$LFC_BAD_IMAGE" -f - "$LFC_CTX" >/dev/null \
  || { rmdir "$LFC_CTX"; fail "could not build the broken-kea-lfc control image"; }
rmdir "$LFC_CTX"
docker rm -f "$LFC_SRV" >/dev/null 2>&1 || true

if run_lfc_server "$LFC_BAD" "$SRV_IP" "$LFC_BAD_IMAGE"; then
  docker run --rm --name "$CLI" --network "$NET" "$TOOLS_IMAGE" \
    /usr/sbin/perfdhcp -4 -R 20 -r 10 -n 40 -p 12 "$SRV_IP" >/dev/null 2>&1 || true
  if wait_for_compaction "$LFC_BAD" 25; then
    fail "negative control PASSED - the gate cannot detect a broken kea-lfc"
  fi
  read_lease_state "$LFC_BAD"
  pass "negative control: broken kea-lfc detected (no compacted file; leftover-.1=$LS_LEFTOVER)"
  # And confirm it really is silent, which is the point of the gate.
  case "$(docker logs "$LFC_BAD" 2>&1 || true)" in
    *ERROR*) printf '  \033[33mnote\033[0m  broken kea-lfc logged an ERROR after all\n' ;;
    *) pass "confirmed: a broken kea-lfc logs no error and stays healthy" ;;
  esac
else
  fail "the broken-kea-lfc control did not start (it is meant to start fine)"
fi
docker rm -f "$LFC_BAD" >/dev/null 2>&1 || true


printf '\n\033[32mAll gates passed.\033[0m\n'
