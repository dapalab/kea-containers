#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Tests for scripts/scan-report.py.
#
# Most days this script's job is to say nothing, so what matters most is
# telling "ran and found nothing" apart from "failed and produced nothing".
# Otherwise a broken scanner would go quiet, and quiet looks like a clean
# bill of health (the same idea as upstream-watch.yml).
#
# The fixtures are written inline rather than recorded from a real scan. A
# recorded one would go stale as the vulnerability database changes, and
# these tests are about the reporting, not grype's findings.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT="$HERE/../scripts/scan-report.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; exit 1; }
info() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

grype_json() {  # grype_json <severity> [<severity> ...]
  python3 -c '
import json, sys
print(json.dumps({
  "descriptor": {"name": "grype", "version": "0.119.0"},
  "source": {"type": "image"},
  "matches": [
    {"vulnerability": {"id": f"CVE-0000-{i}", "severity": s,
                       "fix": {"versions": ["1.2.3"], "state": "fixed"}},
     "artifact": {"name": "somepkg", "version": "1.0.0"}}
    for i, s in enumerate(sys.argv[1:])
  ]}))' "$@"
}

run() {  # run <expected-exit> <label> -- <args...>
  local want="$1" label="$2"; shift 3
  local code=0
  "$REPORT" "$@" --out "$WORK/out.md" >"$WORK/stdout.log" 2>"$WORK/stderr.log" || code=$?
  [ "$code" = "$want" ] || {
    sed 's/^/      | /' "$WORK/stderr.log" "$WORK/stdout.log" 2>/dev/null | head -6
    fail "$label -- exit $code, expected $want"
  }
  pass "$label -- exit $code"
}

###############################################################################
info "Clean scan is exit 0, and writes no issue body"
###############################################################################
grype_json Medium Low Negligible > "$WORK/clean.json"
rm -f "$WORK/out.md"
run 0 "only sub-threshold findings" -- "img:tag=$WORK/clean.json"
[ ! -f "$WORK/out.md" ] || fail "a clean scan wrote an issue body"
pass "clean scan writes no issue body, so nothing is opened"

grype_json > "$WORK/empty.json"
run 0 "no findings at all" -- "img:tag=$WORK/empty.json"

###############################################################################
info "Findings at or above the threshold are exit 2 and reported"
###############################################################################
grype_json High Critical Medium > "$WORK/hits.json"
run 2 "High and Critical present" -- "img:tag=$WORK/hits.json"
grep -q 'CVE-0000-0' "$WORK/out.md" || fail "High finding missing from the report"
grep -q 'CVE-0000-1' "$WORK/out.md" || fail "Critical finding missing from the report"
if grep -q 'CVE-0000-2' "$WORK/out.md"; then
  fail "sub-threshold Medium leaked into the report"
fi
pass "report contains exactly the at-or-above findings"

# Critical should sort above High, so the worst thing is read first.
crit_line="$(grep -n 'CVE-0000-1' "$WORK/out.md" | cut -d: -f1)"
high_line="$(grep -n 'CVE-0000-0' "$WORK/out.md" | cut -d: -f1)"
[ "$crit_line" -lt "$high_line" ] || fail "Critical is listed below High"
pass "Critical sorts above High"

###############################################################################
info "The threshold is honoured"
###############################################################################
# hits.json also has a Critical, so test the raised threshold against a file
# with only a High; otherwise this could pass for the wrong reason.
grype_json High > "$WORK/highonly.json"
run 0 "threshold Critical hides a High" -- "img:tag=$WORK/highonly.json" --threshold Critical
run 2 "threshold High catches that same High" -- "img:tag=$WORK/highonly.json"
grype_json Medium > "$WORK/med.json"
run 2 "threshold Medium catches a Medium" -- "img:tag=$WORK/med.json" --threshold Medium

###############################################################################
info "A BROKEN scan is exit 1 and never looks clean"
###############################################################################
# The most important cases. Each of these has to look different from
# "nothing found"; exit 0 here would file a failure as good news.
echo 'not json'                      > "$WORK/bad1.json"
echo '{"no_matches_key": true}'      > "$WORK/bad2.json"
echo '{"matches":[],"descriptor":{"name":"trivy"}}' > "$WORK/bad3.json"
: > "$WORK/bad4.json"

run 1 "unparseable output"        -- "img:tag=$WORK/bad1.json"
grep -q 'could not run' "$WORK/out.md" || fail "broken scan wrote no explanation"
run 1 "not grype output at all"   -- "img:tag=$WORK/bad2.json"
run 1 "output from another tool"  -- "img:tag=$WORK/bad3.json"
run 1 "empty file (grype crashed)" -- "img:tag=$WORK/bad4.json"
run 1 "file that does not exist"  -- "img:tag=$WORK/nope.json"
run 1 "malformed argument"        -- "no-equals-sign"

###############################################################################
info "One broken target poisons the whole run, rather than being averaged away"
###############################################################################
# One image failing to scan shouldn't be hidden by others scanning clean.
run 1 "clean target + broken target" -- \
  "good=$WORK/clean.json" "broken=$WORK/bad1.json"

###############################################################################
info "A partial scan outranks findings, rather than hiding behind them"
###############################################################################
# What this guards against: one image can't be scanned, another happens to
# have findings, and the run is filed as "vulnerabilities found", so the gap
# in coverage goes unmentioned just because something else was reported.
run 1 "partial + findings -> broken, not 'found'" -- \
  "img:tag=$WORK/hits.json" --partial "kea-dhcp6:3.2 could not be scanned"
grep -q 'scan was incomplete' "$WORK/out.md" \
  || fail "partial run did not banner the report"
grep -q 'CVE-0000-1' "$WORK/out.md" \
  || fail "partial run dropped the findings it did get"
pass "report is bannered AND still lists what was found"

run 1 "partial + no findings -> broken, not clean" -- \
  "img:tag=$WORK/clean.json" --partial "kea-tools:3.0 could not be scanned"
[ -f "$WORK/out.md" ] || fail "partial clean run wrote no issue body"
grep -q 'scan was incomplete' "$WORK/out.md" || fail "no banner on a partial clean run"
pass "a partial scan with nothing found still opens an issue"

run 2 "no --partial is still a normal findings run" -- "img:tag=$WORK/hits.json"
run 0 "empty --partial is treated as not partial" -- "img:tag=$WORK/clean.json" --partial ""

printf '\n\033[32mAll scan-report tests passed.\033[0m\n'
