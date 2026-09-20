#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Verify an ISC source tarball against a detached PGP signature and a
# keyblock, and FAIL CLOSED if the signature is anything other than good.
#
#   usage: verify-tarball.sh <tarball> <detached-sig> <keyblock>
#
# WHY THIS IS NOT JUST `gpg --verify`
#     `gpg --batch --verify` returns EXIT 0 for a signature made by a key that
#     has EXPIRED or been REVOKED. Both were reproduced against GnuPG 2.4.4:
#
#       expired key:  "Good signature ... [expired]"  + exit 0
#       revoked key:  "Good signature ..."            + exit 0
#                     "WARNING: This key has been revoked by its owner!"
#
#     The revoked case is the nastier of the two - the human-readable output
#     still says "Good signature", and the only thing distinguishing it is a
#     warning line in a build log nobody reads.
#
#     On the status-fd interface these cases do NOT emit GOODSIG; they emit
#     EXPKEYSIG and REVKEYSIG instead. GOODSIG is therefore the only reliable
#     signal, and this script asserts its PRESENCE.
#
#     Asserting presence rather than the absence of known-bad tokens is the
#     point: a failure mode GnuPG adds in some future version will also lack
#     GOODSIG, so this assertion stays correct without being updated.
#
# Exercised by test/verify-signature-test.sh, which drives good, expired,
# revoked, untrusted-signer and tampered-payload fixtures through it.
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

# The keyring is built from the vendored keyblock ONLY. A tarball signed by a
# key outside it fails with NO_PUBKEY rather than being fetched on demand.
#
# --no-auto-check-trustdb: we decide on the GOODSIG token, not on the web of
# trust, so building a trustdb would only add noise to the build log. Key
# expiry and revocation are properties of the key itself and are still
# reported (EXPKEYSIG / REVKEYSIG) without it.
gpg --batch --quiet --no-auto-check-trustdb --import "$keyblock"

echo "--- verifying $(basename "$tarball") ---"

# Human-readable pass, for the build log. Its exit code is deliberately
# ignored: it is the status-fd assertion below that decides.
gpg --batch --no-auto-check-trustdb --verify "$sig" "$tarball" || true

# Machine-readable pass. Exit code ignored for the same reason.
gpg --batch --no-auto-check-trustdb --status-fd 3 \
    --verify "$sig" "$tarball" 3>"$work/status" 2>/dev/null || true

if ! grep -q '^\[GNUPG:\] GOODSIG ' "$work/status"; then
    echo "FATAL: signature is NOT a good signature from the vendored ISC keyblock." >&2
    # Name the specific failure where we recognise it. These are diagnostics
    # only - the decision above was already made on the absence of GOODSIG.
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

# Belt and braces: a good signature alongside a bad one must not pass either.
for bad in BADSIG ERRSIG EXPKEYSIG REVKEYSIG EXPSIG; do
    if grep -q "^\[GNUPG:\] $bad " "$work/status"; then
        echo "FATAL: GOODSIG present but so is $bad - refusing an ambiguous result." >&2
        cat "$work/status" >&2
        exit 1
    fi
done

echo "--- signature OK: GOODSIG from the vendored ISC keyblock ---"
