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

EXIT CODES
    0  supported set matches versions.json
    1  could not determine the upstream set - the check itself is broken
    2  the supported set has changed - human decision needed
"""
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

CUR_URL = "https://downloads.isc.org/isc/kea/cur/"
ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.json"
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


def main() -> int:
    configured = set(json.loads(VERSIONS.read_text())["branches"])

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
    pathlib.Path("upstream-report.md").write_text(report)
    print("\n" + report)
    return 2


if __name__ == "__main__":
    sys.exit(main())
