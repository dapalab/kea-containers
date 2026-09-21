#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Negative-control tests for build/verify-tarball.sh.
#
# Why this exists
#     The signature check used to rely on `gpg --batch --verify`, which exits
#     0 for a signature made by an expired or revoked key. So it only
#     rejected the cases it happened to consider (D2).
#
#     A fixed check still needs to be seen failing for the right reason. So
#     each case below runs a fixture that should be rejected through the real
#     script, and fails the test if it's accepted. One case should be
#     accepted, to show the script isn't simply rejecting everything.
#
# It runs in seconds and only needs gpg, so it's in the lint job rather than
# after a Kea compile.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFY="$HERE/../build/verify-tarball.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }
info() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# Each fixture gets its own GNUPGHOME, so nothing carries over between cases.
new_home() {
  local h="$WORK/$1"; rm -rf "$h"; mkdir -p "$h"; chmod 700 "$h"; printf '%s' "$h"
}

# gen <gnupghome> <uid> <expiry> -> prints fingerprint
gen() {
  local h="$1" uid="$2" exp="$3" extra=()
  [ -n "${4:-}" ] && extra=(--faked-system-time "$4")
  GNUPGHOME="$h" gpg --batch --quiet --no-auto-check-trustdb "${extra[@]}" --passphrase '' \
      --pinentry-mode loopback --quick-generate-key "$uid" default default "$exp" 2>/dev/null
  GNUPGHOME="$h" gpg --batch --no-auto-check-trustdb --list-keys --with-colons 2>/dev/null \
    | awk -F: '/^fpr:/{print $10; exit}'
}

sign() {  # sign <gnupghome> <file> <out> [faketime]
  local h="$1" f="$2" o="$3" extra=()
  [ -n "${4:-}" ] && extra=(--faked-system-time "$4")
  GNUPGHOME="$h" gpg --batch --quiet --no-auto-check-trustdb "${extra[@]}" --passphrase '' \
      --pinentry-mode loopback --detach-sign --armor -o "$o" "$f" 2>/dev/null
}

export_key() { GNUPGHOME="$1" gpg --batch --no-auto-check-trustdb --armor --export > "$2" 2>/dev/null; }

# expect_reject <label> <tarball> <sig> <keyblock>
expect_reject() {
  local label="$1"; shift
  if "$VERIFY" "$@" >"$WORK/out.log" 2>&1; then
    sed 's/^/      | /' "$WORK/out.log"
    fail "$label -- ACCEPTED a signature it must reject"
  fi
  pass "$label -- rejected"
}

expect_accept() {
  local label="$1"; shift
  if ! "$VERIFY" "$@" >"$WORK/out.log" 2>&1; then
    sed 's/^/      | /' "$WORK/out.log"
    fail "$label -- REJECTED a signature it must accept"
  fi
  pass "$label -- accepted"
}

echo "payload for signature tests" > "$WORK/kea.tar.xz"
PAST=1704110400   # 2024-01-01, so a 30-day key is long dead by now

###############################################################################
info "Case 1 (positive control): key in the keyblock, valid signature"
###############################################################################
# Without this, every other case could pass just because the script rejects
# everything.
H=$(new_home good); gen "$H" "Good Signer <good@example.invalid>" never >/dev/null
sign "$H" "$WORK/kea.tar.xz" "$WORK/good.asc"
export_key "$H" "$WORK/good.keyblock"
expect_accept "valid signature from a key in the keyblock" \
  "$WORK/kea.tar.xz" "$WORK/good.asc" "$WORK/good.keyblock"

###############################################################################
info "Case 2: EXPIRED signing key (gpg --verify exits 0 here)"
###############################################################################
H=$(new_home exp); gen "$H" "Expired Signer <exp@example.invalid>" 30d "$PAST" >/dev/null
sign "$H" "$WORK/kea.tar.xz" "$WORK/exp.asc" "$PAST"
export_key "$H" "$WORK/exp.keyblock"
expect_reject "signature from an expired key" \
  "$WORK/kea.tar.xz" "$WORK/exp.asc" "$WORK/exp.keyblock"

###############################################################################
info "Case 3: REVOKED signing key (gpg --verify exits 0 and says 'Good')"
###############################################################################
# GnuPG 2.1+ writes a revocation certificate when it creates a key, with a
# ':' at the start of its header so it can't be imported by accident.
# Removing that is the only way to revoke a key in batch mode.
H=$(new_home rev); FPR=$(gen "$H" "Revoked Signer <rev@example.invalid>" never)
sign "$H" "$WORK/kea.tar.xz" "$WORK/rev.asc"
sed 's/^://' "$H/openpgp-revocs.d/$FPR.rev" > "$WORK/revoke.asc"
GNUPGHOME="$H" gpg --batch --quiet --no-auto-check-trustdb --import "$WORK/revoke.asc" 2>/dev/null
export_key "$H" "$WORK/rev.keyblock"
expect_reject "signature from a revoked key" \
  "$WORK/kea.tar.xz" "$WORK/rev.asc" "$WORK/rev.keyblock"

###############################################################################
info "Case 4: signer NOT in the keyblock"
###############################################################################
# The original build already handled this case (NO_PUBKEY, exit 2). It's
# kept so a later change can't quietly break it.
H=$(new_home out); gen "$H" "Outsider <out@example.invalid>" never >/dev/null
sign "$H" "$WORK/kea.tar.xz" "$WORK/out.asc"
expect_reject "signature by a key outside the keyblock" \
  "$WORK/kea.tar.xz" "$WORK/out.asc" "$WORK/good.keyblock"

###############################################################################
info "Case 5: tampered tarball, otherwise-valid signature"
###############################################################################
cp "$WORK/kea.tar.xz" "$WORK/tampered.tar.xz"
echo "malicious addition" >> "$WORK/tampered.tar.xz"
expect_reject "tampered payload against a good signature" \
  "$WORK/tampered.tar.xz" "$WORK/good.asc" "$WORK/good.keyblock"

###############################################################################
info "Case 6: the vendored ISC keyblock imports and holds usable keys"
###############################################################################
# Catches a truncated or damaged keyblock after an update. It doesn't check a
# real tarball; the build does that, against the real signature.
H=$(new_home isc)
GNUPGHOME="$H" gpg --batch --quiet --no-auto-check-trustdb \
  --import "$HERE/../build/keys/isc-keyblock.asc" 2>/dev/null
n=$(GNUPGHOME="$H" gpg --batch --no-auto-check-trustdb --list-keys --with-colons | grep -c '^pub:' || true)
[ "$n" -ge 7 ] || fail "vendored keyblock holds $n keys, expected at least 7"
pass "vendored ISC keyblock imports cleanly ($n keys)"

###############################################################################
info "Case 7: the weekly keyblock warning actually fires"
###############################################################################
# scripts/check-upstream.py warns about revoked or expiring ISC signing keys.
# None of the seven real keys has an expiry date, so on the real keyblock
# that check could only ever say "healthy". So test it with fixtures instead.
WATCHER="$HERE/../scripts/check-upstream.py"

# 7a: a healthy block is reported as healthy (otherwise 7b proves nothing).
H=$(new_home kbok); gen "$H" "Healthy Key <ok@example.invalid>" never >/dev/null
export_key "$H" "$WORK/healthy.keyblock"
if ! "$WATCHER" --check-keyblock "$WORK/healthy.keyblock" >"$WORK/kb.log" 2>&1; then
  sed 's/^/      | /' "$WORK/kb.log"
  fail "healthy keyblock -- reported a problem where there is none"
fi
pass "healthy keyblock -- reported healthy"

# 7b: a revoked key is reported, with exit 3.
H=$(new_home kbrev); FPR=$(gen "$H" "Revoked Key <kbrev@example.invalid>" never)
sed 's/^://' "$H/openpgp-revocs.d/$FPR.rev" > "$WORK/kbrevoke.asc"
GNUPGHOME="$H" gpg --batch --quiet --no-auto-check-trustdb --import "$WORK/kbrevoke.asc" 2>/dev/null
export_key "$H" "$WORK/revoked.keyblock"
set +e
"$WATCHER" --check-keyblock "$WORK/revoked.keyblock" >"$WORK/kb.log" 2>&1
code=$?
set -e
if [ "$code" -ne 3 ]; then
  sed 's/^/      | /' "$WORK/kb.log"
  fail "revoked key in keyblock -- exit $code, expected 3"
fi
grep -q 'REVOKED' "$WORK/kb.log" || fail "revoked key -- exit 3 but no REVOKED in the report"
pass "revoked key in keyblock -- reported, exit 3"

# 7c: a key within 90 days of expiring is reported.
H=$(new_home kbexp); gen "$H" "Expiring Key <kbexp@example.invalid>" 30d >/dev/null
export_key "$H" "$WORK/expiring.keyblock"
set +e
"$WATCHER" --check-keyblock "$WORK/expiring.keyblock" >"$WORK/kb.log" 2>&1
code=$?
set -e
if [ "$code" -ne 3 ]; then
  sed 's/^/      | /' "$WORK/kb.log"
  fail "key expiring in 30 days -- exit $code, expected 3"
fi
grep -q 'EXPIRES in' "$WORK/kb.log" || fail "expiring key -- exit 3 but no EXPIRES in the report"
pass "key expiring inside the 90-day window -- reported, exit 3"

# 7d: an unreadable keyblock is a broken check (exit 1), never "healthy".
set +e
"$WATCHER" --check-keyblock "$WORK/does-not-exist.asc" >"$WORK/kb.log" 2>&1
code=$?
set -e
[ "$code" -eq 1 ] || fail "missing keyblock -- exit $code, expected 1 (broken check)"
pass "missing keyblock -- reported as a broken check, not as healthy"

printf '\n\033[32mAll signature-gate tests passed.\033[0m\n'
