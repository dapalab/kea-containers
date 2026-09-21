#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Functional smoke test for the kea-dhcp4 image.
#
# Six gates, each proving a little more than the last:
#   1. -V              the binary runs and reports its version
#   2. -W              the build report shows the expected crypto backend,
#                      the MySQL/PostgreSQL hooks load, and the SBOM is there
#   3. -t <config>     a real configuration parses
#   4. perfdhcp        a full DHCPv4 exchange (DORA) completes
#   5. every image     each published image starts and reports healthy
#   6. kea-lfc         lease file cleanup really compacts the lease file
#
# Gate 4 is the heart of it: for a DHCP server, proving the binary starts
# isn't enough.
set -euo pipefail

DHCP4_IMAGE="${DHCP4_IMAGE:-kea-dhcp4:test}"
TOOLS_IMAGE="${TOOLS_IMAGE:-kea-tools:test}"
# Gate 5 checks the other published images too. Their names come from
# DHCP4_IMAGE, so one variable still drives everything, whether local :test
# tags or published ones.
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

# Every image should include the SPDX document describing Kea itself. Without
# it, the published SBOM lists 25 apk packages and leaves out the DHCP server.
# See build/make-sbom.sh and docs/DECISIONS.md D23.
check_sbom_doc() {  # check_sbom_doc <image-ref> <label>
  local doc err
  doc="$(docker run --rm --entrypoint cat "$1" /usr/share/sbom/kea.spdx.json 2>/dev/null)" \
    || fail "$2: /usr/share/sbom/kea.spdx.json is missing from the image"
  # stderr is kept rather than thrown away: an earlier version sent it to
  # /dev/null, which hid a syntax error in its own validator, so the check
  # failed for a reason nobody could see.
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

# Kea is compiled from source, so buildx's SBOM scanner can't see it; the
# image includes an SPDX document that adds it to the SBOM. Checked here,
# before publishing, because the scanner silently skips a document it can't
# read, and Kea would quietly drop out of the SBOM.
if [ -n "$EXPECT_VERSION" ]; then
  check_sbom_doc "$DHCP4_IMAGE" "dhcp4"
else
  printf '  \033[33mskip\033[0m  SBOM document check needs EXPECT_VERSION\n'
fi

# The database backends are built in. The server links libmariadb and libpq
# directly and never runs the mysql/psql command-line tools, which is why
# those aren't in the server images. See docs/DECISIONS.md D13.
check_report "MySQL backend compiled in"      'MySQL:[[:space:]]+[^n]'
check_report "PostgreSQL backend compiled in" 'PostgreSQL:[[:space:]]+[^n]'

# In Kea 3.x, each database backend is a hook library rather than part of
# the server. That's why `kea-dhcp4 -V` lists only memfile: it shows the
# backends that are loaded, and the database ones load with their hook.
#
# What doesn't work as a test: `-t` with "type": "mysql" and no hook exits 0,
# because -t checks the backend's name against a known list, not whether it's
# available. (A made-up name like "notarealdb" does exit 1.) So an accepted
# config says nothing about whether the backend was built.
#
# What does tell the difference, tested against a real image:
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

  # Negative control first: a hook path that doesn't exist has to fail, or
  # the positive check below could pass for the wrong reason.
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

# Negative control: -t has to reject bad input, or gate 3 wouldn't prove
# anything.
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

# Wait for the server to say it's ready, rather than sleeping for a guess.
#
# Logs go into a variable before matching. Piping `docker logs` into
# `grep -q` looks natural but goes wrong under `set -o pipefail`: grep stops at
# the first match, docker logs gets SIGPIPE, and the pipeline reports failure
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

# -R 20 simulates 20 different clients. Without it, perfdhcp reuses one MAC,
# so the server answers every request with the same address
# (DHCP4_LEASE_REUSE in the logs), and the test only proves one exchange
# works, not that the pool hands out addresses. With it, we can check that
# different addresses were handed out.
set +e
PERF="$(docker run --rm --name "$CLI" --network "$NET" "$TOOLS_IMAGE" \
  /usr/sbin/perfdhcp -4 -R 20 -r 10 -n 50 -p 20 "$SRV_IP" 2>&1)"
PERF_RC=$?
set -e
printf '%s\n' "$PERF" | sed 's/^/      /'

# perfdhcp's exit code isn't a useful pass/fail here: it returns 3 if any
# packet was dropped, and a single warm-up drop does that. So read the
# statistics for each exchange and check the one that matters.
#
# DISCOVER-OFFER shows the server answers. REQUEST-ACK shows the full
# four-step exchange completed and a lease was actually recorded.
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

# Allow a little loss at the start rather than demanding a perfect run: the
# first DISCOVER can go out before the server has finished binding. Anything
# below 90% means something really is wrong.
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

# Count different addresses in the lease file, not rows: memfile adds a row
# for every lease update, so a row count can look fine while the server has
# been handing the same address to one client over and over.
CSV="$(docker exec "$SRV" cat /var/lib/kea/kea-leases4.csv 2>/dev/null || true)"
DISTINCT="$(printf '%s\n' "$CSV" | tail -n +2 | cut -d, -f1 | grep -cE '^172\.31\.77\.' || true)"
UNIQUE="$(printf '%s\n' "$CSV" | tail -n +2 | cut -d, -f1 | grep -E '^172\.31\.77\.' | sort -u | wc -l)"
: "${UNIQUE:=0}"

[ "$UNIQUE" -ge 5 ] \
  || fail "only $UNIQUE distinct address(es) allocated - the pool is not allocating"
pass "$UNIQUE distinct addresses allocated from the pool ($DISTINCT lease rows)"

# Every address should be inside the configured pool, 172.31.77.100-200.
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
# Gates 1-4 only test kea-dhcp4, with kea-tools running perfdhcp. Without
# this gate, a completely broken kea-dhcp6, kea-dhcp-ddns or kea-ctrl-agent
# could pass CI and be published, because "it compiled" was the only check.
#
# Each image starts with the default config it ships with, which also shows
# those templates really work, not just that they parse.
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

# ctrl-agent only exists on the 3.0 branch. Its healthcheck is an HTTP
# request rather than a Unix socket check, and this is the only place it runs
# against a real agent.
ca_img="${IMAGE_PREFIX}ctrl-agent${IMAGE_SUFFIX}"
if docker image inspect "$ca_img" >/dev/null 2>&1 || docker pull -q "$ca_img" >/dev/null 2>&1; then
  check_starts ctrl-agent CTRL_AGENT_STARTED health
else
  printf '  \033[33mskip\033[0m  kea-ctrl-agent not built for this branch\n'
fi

###############################################################################
info "Gate 6: kea-lfc actually compacts the lease file"
###############################################################################
# Why this gate exists
#     kea-lfc is in the dhcp4, dhcp6 and tools images, and both templates run
#     it hourly, but nothing ever tested it. That's the same pattern as the
#     kea-ctrl-agent bug: present, configured, and never actually run.
#
# What actually goes wrong (tested)
#     A missing kea-lfc isn't the risk: kea-dhcp4 won't start without it
#     ("DHCPSRV_MEMFILE_FAILED_TO_OPEN ... File not found: /usr/sbin/
#     kea-lfc", DHCP4_INIT_FAIL), so gates 4 and 5 already catch that.
#
#     The real risk is a kea-lfc that's present but broken: a missing shared
#     library, the wrong architecture, a directory it can't write to. Kea
#     runs it and never checks whether it succeeded, so:
#
#         container            : healthy
#         logs                 : LFC_START / LFC_EXECUTE, no error at all
#         lease file           : keeps growing
#
#     Nothing shows until the lease file has grown for weeks and restarts
#     slow down.
#
# How to tell (measured)
#     working kea-lfc : kea-leases4.csv.2 holds the compacted leases (one row
#                       per address), with no .1 left behind
#     broken kea-lfc  : no .2 at all, an unprocessed .1 and a stale .pid
#
#     So the check is that .2 exists and is compacted, which can only happen
#     if kea-lfc ran to completion. See D22.
# Removed by cleanup(). It doesn't get its own trap on purpose: that would
# replace the existing EXIT trap and leave the containers below running.
LFC_CONF="$(mktemp)"
# mktemp creates files as 0600. The server runs as UID 10000, not as whoever
# runs this script, so a bind-mounted 0600 config can't be read inside the
# container, and Kea stops with "Unable to open file".
chmod 0644 "$LFC_CONF"
# Made from the gate-4 config with sed rather than kept as a second file, so
# the two can't drift apart.
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
# so this normally settles in about 10s, and a slow runner won't make it
# flaky.
wait_for_compaction() {  # wait_for_compaction <container> <seconds>
  local container="$1" limit="$2" n=0
  while [ "$n" -lt "$limit" ]; do
    read_lease_state "$container"
    # Compacted means: a .2 exists, it holds the leases we added, and it has
    # exactly one row per address. A file with duplicate rows is a rotation
    # kea-lfc never finished processing.
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

# Negative control: the check above has to be shown to fail when kea-lfc is
# broken, since that's what it's for. kea-lfc is replaced with a stub that
# exits 1: present and runnable, so Kea starts normally and reports healthy.
# The build context is empty on purpose: the Dockerfile has no COPY, and the
# repository root would send .git and everything else for nothing.
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
  # And confirm the failure really is silent, which is why the gate exists.
  case "$(docker logs "$LFC_BAD" 2>&1 || true)" in
    *ERROR*) printf '  \033[33mnote\033[0m  broken kea-lfc logged an ERROR after all\n' ;;
    *) pass "confirmed: a broken kea-lfc logs no error and stays healthy" ;;
  esac
else
  fail "the broken-kea-lfc control did not start (it is meant to start fine)"
fi
docker rm -f "$LFC_BAD" >/dev/null 2>&1 || true


printf '\n\033[32mAll gates passed.\033[0m\n'
