#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Tests for build/make-sbom.sh.
#
# The failure this guards against is silent: BuildKit's scanner skips an SBOM
# document it cannot parse, so a malformed one puts us straight back to
# publishing an SBOM with no Kea in it - and the build stays green. So the
# generator validates its own output, and these tests prove that validation
# rejects what it should.
#
# Needs only sh, sha256sum and python3, so it runs in lint rather than behind
# a Kea compile.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="$HERE/../build/make-sbom.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }
info() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

URL="https://downloads.isc.org/isc/kea/3.2.0/kea-3.2.0.tar.xz"
echo "pretend tarball" > "$WORK/kea.tar.xz"
EXPECT_SHA="$(sha256sum "$WORK/kea.tar.xz" | cut -d' ' -f1)"

###############################################################################
info "Valid input produces a document that names Kea"
###############################################################################
"$GEN" 3.2.0 "$WORK/kea.tar.xz" "$URL" "$WORK/out/kea.spdx.json" >/dev/null
[ -f "$WORK/out/kea.spdx.json" ] || fail "no document written"
python3 - "$WORK/out/kea.spdx.json" "$EXPECT_SHA" "$URL" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
p = d["packages"][0]
assert d["spdxVersion"] == "SPDX-2.3", d["spdxVersion"]
assert p["name"] == "kea", p["name"]
assert p["versionInfo"] == "3.2.0", p["versionInfo"]
assert p["licenseDeclared"] == "MPL-2.0", p["licenseDeclared"]
assert p["downloadLocation"] == sys.argv[3], p["downloadLocation"]
assert p["checksums"][0]["checksumValue"] == sys.argv[2], p["checksums"]
locs = [r["referenceLocator"] for r in p["externalRefs"]]
assert any(l.startswith("cpe:2.3:a:isc:kea:3.2.0") for l in locs), locs
assert any(l.startswith("pkg:generic/kea@3.2.0") for l in locs), locs
PY
pass "valid SPDX naming kea 3.2.0, MPL-2.0, with checksum, CPE and purl"

###############################################################################
info "The recorded checksum is the tarball's, not a constant"
###############################################################################
# A hash that never changes would be the sha256sum bug again in a new place.
echo "different bytes entirely" > "$WORK/other.tar.xz"
"$GEN" 3.2.0 "$WORK/other.tar.xz" "$URL" "$WORK/out2.spdx.json" >/dev/null
A="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["packages"][0]["checksums"][0]["checksumValue"])' "$WORK/out/kea.spdx.json")"
B="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["packages"][0]["checksums"][0]["checksumValue"])' "$WORK/out2.spdx.json")"
[ "$A" != "$B" ] || fail "different tarballs produced the same checksum"
[ "$A" = "$EXPECT_SHA" ] || fail "checksum does not match sha256sum of the input"
pass "checksum tracks the actual bytes ($A != $B)"

###############################################################################
info "Output is reproducible, so it cannot break the tested==published check"
###############################################################################
# A timestamp here would change the layer between the test build and the push
# build and trip the digest assertion in build.yml (D21).
"$GEN" 3.2.0 "$WORK/kea.tar.xz" "$URL" "$WORK/rep1.json" >/dev/null
sleep 1
"$GEN" 3.2.0 "$WORK/kea.tar.xz" "$URL" "$WORK/rep2.json" >/dev/null
cmp -s "$WORK/rep1.json" "$WORK/rep2.json" \
  || { diff "$WORK/rep1.json" "$WORK/rep2.json" | head -5 | sed 's/^/      /'
       fail "two runs a second apart produced different bytes"; }
pass "byte-identical across runs"

###############################################################################
info "Bad input is refused"
###############################################################################
expect_fail() {  # expect_fail <label> <args...>
  local label="$1"; shift
  if "$GEN" "$@" >"$WORK/err.log" 2>&1; then
    sed 's/^/      | /' "$WORK/err.log"
    fail "$label -- accepted"
  fi
  pass "$label -- refused"
}
expect_fail "version that is not X.Y.Z"  "3.2"    "$WORK/kea.tar.xz" "$URL" "$WORK/x.json"
expect_fail "empty version"              ""       "$WORK/kea.tar.xz" "$URL" "$WORK/x.json"
expect_fail "tarball that does not exist" "3.2.0" "$WORK/missing.xz" "$URL" "$WORK/x.json"
expect_fail "missing output argument"    "3.2.0"  "$WORK/kea.tar.xz" "$URL"

###############################################################################
info "The shipped-document validator rejects what it should"
###############################################################################
# check-sbom-doc.py is what the smoke test runs against every image. It began
# life as an inline `python3 -c` with nested quote escaping, got the escaping
# wrong, and the resulting syntax error was invisible because the caller sent
# stderr to /dev/null - the assertion failed for a reason nobody could see.
# Hence a real file, and hence these cases.
VALIDATOR="$HERE/check-sbom-doc.py"
GOOD="$WORK/out/kea.spdx.json"

"$VALIDATOR" 3.2.0 < "$GOOD" >/dev/null || fail "rejected a valid document"
pass "accepts the document make-sbom.sh just produced"

# Document arrives on STDIN. An earlier version took a command string and
# tried to run it as a single word, so every case failed with "not valid
# JSON" - three green ticks, none of them testing what they claimed.
# Each expected message is asserted, not just the exit status.
reject() {  # reject <label> <version> <expected-message-substring>
  local label="$1" version="$2" want="$3"
  if "$VALIDATOR" "$version" >"$WORK/v.log" 2>&1; then
    sed 's/^/      | /' "$WORK/v.log"
    fail "$label -- accepted"
  fi
  grep -qF "$want" "$WORK/v.log" \
    || { sed 's/^/      | /' "$WORK/v.log"
         fail "$label -- rejected, but not for the stated reason (wanted: $want)"; }
  pass "$label -- rejected: $(head -1 "$WORK/v.log")"
}
reject "version disagrees with the image" 3.0.4 "image is '3.0.4'"   < "$GOOD"
echo 'nonsense' | reject "not JSON at all" 3.2.0 "not valid JSON"
echo '{"packages":[{"name":"busybox"}]}' \
  | reject "no kea package" 3.2.0 "found 0"

strip() { python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
d['packages'][0][sys.argv[2]] = [] if sys.argv[2] in ('externalRefs','checksums') else 'x'
print(json.dumps(d))" "$GOOD" "$1"; }
for field in externalRefs checksums licenseDeclared; do
  case "$field" in
    externalRefs)    want="no CPE" ;;
    checksums)       want="source checksum missing" ;;
    licenseDeclared) want="expected MPL-2.0" ;;
  esac
  strip "$field" | reject "broken $field" 3.2.0 "$want"
done

printf '\n\033[32mAll make-sbom tests passed.\033[0m\n'
