#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Check an ISC source tarball against a detached PGP signature and a
# keyblock, and fail unless the signature is good.
#
#   usage: verify-tarball.sh <tarball> <detached-sig> <keyblock>
#
# Why this isn't just `gpg --verify`
#     `gpg --batch --verify` exits 0 for a signature made by a key that has
#     expired or been revoked. Both reproduced with GnuPG 2.4.4:
#
#       expired key:  "Good signature ... [expired]"  + exit 0
#       revoked key:  "Good signature ..."            + exit 0
#                     "WARNING: This key has been revoked by its owner!"
#
#     The revoked case is the more worrying one: gpg still prints "Good
#     signature", and only a warning line in the build log tells them apart.
#
#     On the status-fd output, these cases don't report GOODSIG; they report
#     EXPKEYSIG and REVKEYSIG instead. So GOODSIG is the reliable signal, and
#     this script checks that it's present.
#
#     Checking for GOODSIG, rather than for known-bad tokens, means any new
#     failure GnuPG adds in future will also fail here, with no changes
#     needed. See D2.
#
# Tested by test/verify-signature-test.sh, which runs good, expired, revoked,
# untrusted-signer and tampered-payload fixtures through it.
set -eu

tarball="${1:?usage: verify-tarball.sh <tarball> <sig> <keyblock>}"
sig="${2:?usage: verify-tarball.sh <tarball> <sig> <keyblock>}"
keyblock="${3:?usage: verify-tarball.sh <tarball> <sig> <keyblock>}"

for f in "$tarball" "$sig" "$keyblock"; do
    [ -f "$f" ] || { echo "FATAL: no such file: $f" >&2; exit 1; }
done

work="$(mktemp -d)"
GNUPGHOME="$work/gnupg"
export GNUPGHOME
mkdir -p "$GNUPGHOME"
chmod 700 "$GNUPGHOME"
# shellcheck disable=SC2064  # $work must expand now, not at trap time
trap "rm -rf '$work'" EXIT INT TERM

# The keyring is built from the stored keyblock only. A tarball signed by any
# other key fails with NO_PUBKEY; gpg won't go and fetch the key.
#
# --no-auto-check-trustdb: the decision rests on GOODSIG, not the web of
# trust, so building a trustdb would only add noise to the build log. Expiry
# and revocation belong to the key itself, and are still reported
# (EXPKEYSIG / REVKEYSIG) without it.
gpg --batch --quiet --no-auto-check-trustdb --import "$keyblock"

echo "--- verifying $(basename "$tarball") ---"

# A human-readable run, for the build log. Its exit code is ignored on
# purpose: the status-fd check below makes the decision.
gpg --batch --no-auto-check-trustdb --verify "$sig" "$tarball" || true

# Machine-readable pass. Exit code ignored for the same reason.
gpg --batch --no-auto-check-trustdb --status-fd 3 \
    --verify "$sig" "$tarball" 3>"$work/status" 2>/dev/null || true

if ! grep -q '^\[GNUPG:\] GOODSIG ' "$work/status"; then
    echo "FATAL: signature is NOT a good signature from the vendored ISC keyblock." >&2
    # Name the specific problem where we recognise it. This is just for the
    # log: the decision was already made above, on GOODSIG being missing.
    if   grep -q '^\[GNUPG:\] EXPKEYSIG ' "$work/status"; then
        echo "       cause: the signing key has EXPIRED." >&2
    elif grep -q '^\[GNUPG:\] REVKEYSIG ' "$work/status"; then
        echo "       cause: the signing key has been REVOKED by its owner." >&2
    elif grep -q '^\[GNUPG:\] NO_PUBKEY ' "$work/status"; then
        echo "       cause: signed by a key that is NOT in the vendored keyblock." >&2
        echo "       If ISC has added a signer, re-vendor build/keys/isc-keyblock.asc" >&2
        echo "       deliberately - see build/keys/README.md." >&2
    elif grep -q '^\[GNUPG:\] BADSIG ' "$work/status"; then
        echo "       cause: BAD signature - the tarball does not match the signature." >&2
    elif grep -q '^\[GNUPG:\] EXPSIG ' "$work/status"; then
        echo "       cause: the signature itself has expired." >&2
    fi
    echo "--- gpg status output ---" >&2
    cat "$work/status" >&2
    exit 1
fi

# Belt and braces: a good signature alongside a bad one shouldn't pass either.
for bad in BADSIG ERRSIG EXPKEYSIG REVKEYSIG EXPSIG; do
    if grep -q "^\[GNUPG:\] $bad " "$work/status"; then
        echo "FATAL: GOODSIG present but so is $bad - refusing an ambiguous result." >&2
        cat "$work/status" >&2
        exit 1
    fi
done

echo "--- signature OK: GOODSIG from the vendored ISC keyblock ---"
