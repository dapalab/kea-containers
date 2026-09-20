#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Detect changes in the set of Kea branches ISC currently supports.

WHY THIS EXISTS
    Renovate tracks versions but knows nothing about ISC's support policy. It
    will happily bump 3.2.0 -> 3.2.1 forever and never tell us that 3.4 has
    appeared or that 3.0 has reached EOL. That gap is what this closes.

WHAT IT WATCHES
    https://downloads.isc.org/isc/kea/cur/ - an Apache directory index listing
    one directory per branch ISC currently maintains. This is deliberately NOT
    the isc.org/kea marketing page: an autoindex is structurally stable, while
    a marketing page is restyled whenever someone feels like it.

    Even minor = stable, odd = development. We ship stable only.

HOW BRITTLE IS IT, HONESTLY
    Moderately. It depends on two things:

      1. ISC keeping /isc/kea/cur/ as a browsable directory index.
      2. Directory names staying in X.Y form.

    Both have held for years and neither is a presentation choice, so this is
    considerably sturdier than parsing prose. But it is still scraping, and it
    WILL break eventually.

    THE FAILURE MODE IS THE IMPORTANT PART. A scraper that silently stops
    matching is worse than no scraper, because silence reads as "nothing has
    changed". So this script treats "I could not parse that" as a loud failure
    (exit 1), distinct from "the set changed" (exit 2) and "all is well"
    (exit 0). The workflow opens an issue for BOTH non-zero cases. Silence
    therefore always means the check ran and genuinely found nothing.

WHAT ELSE IT WATCHES
    The vendored PGP keyblock at build/keys/isc-keyblock.asc. The build gate
    asserts a GOODSIG status token, so a revoked or expired ISC signing key
    now stops the build - correctly, but with no warning beforehand. This runs
    weekly and is the warning: it reports a key that is already revoked or
    expired, and warns 90 days ahead of an expiry.

    As of 2026-09-20 none of the seven keys carries an expiry date, so the
    90-day warning is inert today. It exists because that is a property of the
    current block, not a guarantee - a re-vendored block may differ.

EXIT CODES
    0  supported set matches versions.json, keyblock healthy
    1  could not determine the upstream set - the check itself is broken
    2  the supported set has changed - human decision needed
    3  the vendored keyblock needs attention - key revoked, expired or expiring
"""
import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

CUR_URL = "https://downloads.isc.org/isc/kea/cur/"
ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.json"
KEYBLOCK = ROOT / "build" / "keys" / "isc-keyblock.asc"
EXPIRY_WARN_DAYS = 90
UA = "kea-containers-upstream-watcher (+https://github.com/dapalab/kea-containers)"

# Matches the href of a branch directory in the autoindex, e.g. href="3.2/"
BRANCH_HREF = re.compile(r'href="(\d+\.\d+)/"')
TARBALL_HREF = re.compile(r'href="kea-(\d+\.\d+\.\d+)\.tar\.xz"')


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def upstream_branches() -> set[str]:
    """Branches ISC currently maintains, per the download area."""
    html = fetch(CUR_URL)
    found = set(BRANCH_HREF.findall(html))

    # Sanity checks. If the page changed shape we want to KNOW, not to
    # quietly conclude that ISC supports nothing.
    if not found:
        raise RuntimeError(
            f"no branch directories matched at {CUR_URL}. "
            "The index format has probably changed - this check needs updating."
        )
    if len(found) < 2:
        raise RuntimeError(
            f"only found {found} at {CUR_URL}. ISC has maintained at least two "
            "branches concurrently for years; this is more likely a parse "
            "failure than a real change."
        )
    if not any(int(b.split(".")[1]) % 2 == 0 for b in found):
        raise RuntimeError(
            f"found {sorted(found)} but none has an even minor version. "
            "There is always at least one stable branch - suspect a parse failure."
        )
    return found


def latest_patch(branch: str) -> str | None:
    """Newest tarball in a branch directory, for the issue body."""
    try:
        versions = TARBALL_HREF.findall(fetch(f"{CUR_URL}{branch}/"))
    except Exception:
        return None
    if not versions:
        return None
    return max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))


def keyblock_report(problems: list[str]) -> str:
    """Issue body for a keyblock finding."""
    lines = ["## The vendored ISC keyblock needs attention", ""]
    lines += [f"- {p}" for p in problems]
    lines += [
        "",
        "`build/keys/isc-keyblock.asc` is what the build verifies the Kea "
        "source tarball against. The gate asserts a `GOODSIG` status token, so "
        "a revoked or expired key does **not** quietly pass - it stops the "
        "build. That is correct, and it is also why this warning exists: "
        "without it the first symptom would be a failed release build.",
        "",
        "Re-vendoring is deliberately manual. Follow "
        "`build/keys/README.md`, confirm any new key out of band, and update "
        "the fingerprint table in the same commit.",
        "",
    ]
    return "\n".join(lines) + "\n"


def keyblock_health() -> tuple[list[str], list[str]]:
    """Inspect the vendored keyblock.

    Returns (problems, notes). A non-empty problems list means a key is
    revoked, expired, or expiring within EXPIRY_WARN_DAYS - all of which need
    a human, because re-vendoring the block is deliberately a manual act.

    Raises RuntimeError if the block cannot be read at all, which is a broken
    check rather than a finding: it must be loud, not silently "healthy".
    """
    if not KEYBLOCK.is_file():
        raise RuntimeError(f"vendored keyblock missing: {KEYBLOCK}")
    if shutil.which("gpg") is None:
        raise RuntimeError("gpg is not installed, cannot inspect the keyblock")

    with tempfile.TemporaryDirectory() as home:
        pathlib.Path(home).chmod(0o700)
        env = {"GNUPGHOME": home, "PATH": "/usr/bin:/bin:/usr/local/bin"}
        imp = subprocess.run(["gpg", "--batch", "--quiet", "--no-auto-check-trustdb",
                              "--import", str(KEYBLOCK)],
                             env=env, capture_output=True, text=True)
        if imp.returncode != 0:
            raise RuntimeError(f"gpg could not import the keyblock: {imp.stderr.strip()}")
        listing = subprocess.run(
            ["gpg", "--batch", "--no-auto-check-trustdb", "--list-keys",
             "--with-colons", "--fixed-list-mode"],
            env=env, capture_output=True, text=True, check=True).stdout

    problems: list[str] = []
    notes: list[str] = []
    now = datetime.datetime.now(datetime.timezone.utc)
    total = 0
    fpr = uid = None
    pending: dict | None = None

    for line in listing.splitlines():
        f = line.split(":")
        if f[0] == "pub":
            total += 1
            pending = {"validity": f[1], "expires": f[6]}
            fpr = uid = None
        elif f[0] == "fpr" and pending is not None and fpr is None:
            fpr = f[9]
        elif f[0] == "uid" and pending is not None and uid is None:
            uid = f[9]
            short = f"{uid} ({fpr[-16:] if fpr else '?'})"
            # Validity flags: r = revoked, e = expired, i = invalid.
            if pending["validity"] == "r":
                problems.append(f"**REVOKED**: {short}")
            elif pending["validity"] == "e":
                problems.append(f"**EXPIRED**: {short}")
            elif pending["validity"] == "i":
                problems.append(f"**INVALID**: {short}")
            elif pending["expires"]:
                left = (datetime.datetime.fromtimestamp(
                    int(pending["expires"]), datetime.timezone.utc) - now).days
                when = datetime.datetime.fromtimestamp(
                    int(pending["expires"]), datetime.timezone.utc).date()
                if left <= EXPIRY_WARN_DAYS:
                    problems.append(f"**EXPIRES in {left} days** ({when}): {short}")
                else:
                    notes.append(f"{short} - expires {when} ({left} days)")
            else:
                notes.append(f"{short} - no expiry")
            pending = None

    if total == 0:
        raise RuntimeError("the vendored keyblock contains no public keys")
    notes.insert(0, f"{total} keys in build/keys/isc-keyblock.asc")
    return problems, notes


def main() -> int:
    configured = set(json.loads(VERSIONS.read_text())["branches"])

    # Keyblock first: it is cheap, local, and a revoked signing key matters
    # more than a branch-set change. A failure to inspect it at all is a
    # broken check (exit 1), not a clean bill of health.
    try:
        key_problems, key_notes = keyblock_health()
    except (RuntimeError, subprocess.SubprocessError, OSError) as e:
        print(f"WATCHER BROKEN: keyblock check failed: {e}", file=sys.stderr)
        pathlib.Path("upstream-report.md").write_text(
            "## Upstream watcher could not inspect the vendored keyblock\n\n"
            f"```\n{e}\n```\n\n"
            "This is a failure of the **check**, not necessarily a problem "
            "with the keys. While it persists, nothing is warning us that an "
            "ISC signing key has been revoked or is about to expire - and the "
            "build gate will simply start failing when it happens.\n\n"
            "Fix `scripts/check-upstream.py` or `build/keys/isc-keyblock.asc`.\n"
        )
        return 1

    print("keyblock:")
    for n in key_notes:
        print(f"  {n}")
    for pr in key_problems:
        print(f"  ATTENTION: {pr}")
    print()

    try:
        found = upstream_branches()
    except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
        print(f"WATCHER BROKEN: {e}", file=sys.stderr)
        pathlib.Path("upstream-report.md").write_text(
            "## Upstream watcher could not determine ISC's supported set\n\n"
            f"```\n{e}\n```\n\n"
            f"Source: {CUR_URL}\n\n"
            "This is a failure of the **check**, not necessarily a change "
            "upstream. Until it is fixed, nothing is watching ISC's support "
            "policy, so treat it as actionable rather than noise.\n\n"
            "Fix `scripts/check-upstream.py`, or replace the signal if ISC has "
            "restructured the download area.\n"
        )
        return 1

    stable = {b for b in found if int(b.split(".")[1]) % 2 == 0}
    dev = found - stable

    print(f"upstream maintains : {' '.join(sorted(found))}")
    print(f"  stable           : {' '.join(sorted(stable))}")
    print(f"  development      : {' '.join(sorted(dev))}  (not shipped)")
    print(f"configured here    : {' '.join(sorted(configured))}")

    added = stable - configured
    removed = configured - stable

    if not added and not removed:
        if key_problems:
            pathlib.Path("upstream-report.md").write_text(keyblock_report(key_problems))
            print("\nSupported set unchanged, but the keyblock needs attention.")
            return 3
        print("\nNo change. Supported set matches versions.json.")
        return 0

    lines = ["## ISC's supported Kea branches have changed", ""]
    if added:
        lines += ["### New stable branch(es) upstream", ""]
        for b in sorted(added):
            lines.append(f"- **{b}** - latest release `{latest_patch(b) or 'unknown'}`")
        lines += ["", "Add to `versions.json` if we intend to ship it. Remember "
                      "to set the per-branch `images` list: `kea-ctrl-agent` "
                      "exists only in 3.0.", ""]
    if removed:
        lines += ["### Branch(es) we ship that upstream no longer lists", ""]
        for b in sorted(removed):
            lines.append(f"- **{b}** - likely EOL")
        lines += ["", "Confirm against https://www.isc.org/kea/ before removing. "
                      "Once retired, drop the entry from `versions.json` and "
                      "decide what to do with the published tags - they should "
                      "probably stay pullable but stop being rebuilt.", ""]
    lines += ["---", "",
              f"- upstream maintains: `{' '.join(sorted(found))}`",
              f"- of those, stable: `{' '.join(sorted(stable))}`",
              f"- configured here: `{' '.join(sorted(configured))}`",
              f"- source: {CUR_URL}", "",
              "_Opened automatically by the upstream watcher. Renovate cannot "
              "see ISC's support policy, only version numbers._"]
    report = "\n".join(lines) + "\n"
    if key_problems:
        # Both fired in the same week. Report both in one issue rather than
        # dropping one, and return the more severe code.
        report += "\n" + keyblock_report(key_problems)
    pathlib.Path("upstream-report.md").write_text(report)
    print("\n" + report)
    return 3 if key_problems else 2


def check_keyblock_only(path: str | None) -> int:
    """`--check-keyblock [PATH]`: inspect a keyblock and nothing else.

    Exists so the keyblock check is runnable on its own - after re-vendoring,
    and by test/verify-signature-test.sh against deliberately revoked and
    expired fixtures. A weekly warning that has never been shown to fire is
    not a warning.
    """
    global KEYBLOCK
    if path:
        KEYBLOCK = pathlib.Path(path)
    try:
        problems, notes = keyblock_health()
    except (RuntimeError, subprocess.SubprocessError, OSError) as e:
        print(f"keyblock check failed: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print(f"  {n}")
    for pr in problems:
        print(f"  ATTENTION: {pr}")
    return 3 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-keyblock":
        sys.exit(check_keyblock_only(sys.argv[2] if len(sys.argv) > 2 else None))
    sys.exit(main())
