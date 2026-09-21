#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Turn grype JSON into a GitHub issue body, and decide whether to raise one.

Why a script rather than jq in the workflow
    The important behaviour is what happens when the scan finds nothing,
    which is most days. Silence has to mean "ran and found nothing", never
    "the scanner failed and the error got lost". That's worth testing, and
    workflow YAML can't be tested.

Why two scans per image
    grype scanning an image lists apk packages, but doesn't read the SPDX
    document inside the image. Tested with an image containing a deliberately
    old Kea: the image scan found 0 Kea CVEs, and the document scan found 3.
    Kea is compiled from source, so without the second scan, the component
    that matters most here wouldn't be checked.

Why grype and not trivy
    Measured against an SBOM naming Kea 1.4.0, which has known CVEs:

        trivy sbom  -> 0 findings; --list-all-pkgs shows it parses no
                       packages at all from the document
        grype sbom  -> CVE-2018-5739, CVE-2019-6472, CVE-2019-6474

    grype can match CPEs for software it can't place in a package ecosystem,
    which is exactly the case for anything built from a tarball; trivy
    can't. The CPE in the SBOM (D23) is what makes this work.

Exit codes
    0  scanned cleanly, nothing at or above the threshold
    1  the check itself is broken - missing, unparseable or non-grype input
    2  findings at or above the threshold
"""
import argparse
import json
import pathlib
import sys

# grype capitalises these.
ORDER = ["Unknown", "Negligible", "Low", "Medium", "High", "Critical"]


def load(path: pathlib.Path) -> dict:
    """Read one grype report, and reject anything that isn't one."""
    if not path.is_file():
        raise RuntimeError(f"no such scan output: {path}")
    if path.stat().st_size == 0:
        raise RuntimeError(f"scan output is empty: {path} (did grype run?)")
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path}: not valid JSON: {e}") from e
    if not isinstance(doc, dict) or "matches" not in doc:
        raise RuntimeError(f"{path}: no 'matches' key - this is not grype output")
    if (doc.get("descriptor") or {}).get("name") not in ("grype", None):
        raise RuntimeError(f"{path}: descriptor says {doc['descriptor'].get('name')!r}")
    return doc


def findings(doc: dict, threshold: str) -> list[dict]:
    cut = ORDER.index(threshold)
    out = []
    for m in doc.get("matches") or []:
        vuln = m.get("vulnerability") or {}
        sev = vuln.get("severity", "Unknown")
        if sev not in ORDER or ORDER.index(sev) < cut:
            continue
        art = m.get("artifact") or {}
        fix = (vuln.get("fix") or {})
        out.append({
            "id": vuln.get("id", "?"),
            "severity": sev,
            "package": art.get("name", "?"),
            "version": art.get("version", "?"),
            "fix": ", ".join(fix.get("versions") or []) or (fix.get("state") or "none"),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", metavar="LABEL=FILE",
                    help="grype JSON, labelled with what was scanned")
    ap.add_argument("--threshold", default="High", choices=ORDER)
    ap.add_argument("--out", default="scan-report.md")
    ap.add_argument("--partial", default="",
                    help="reason some target could not be scanned; forces the "
                         "broken exit code and banners the report")
    a = ap.parse_args()

    per_target: dict[str, list[dict]] = {}
    try:
        for spec in a.inputs:
            if "=" not in spec:
                raise RuntimeError(f"expected LABEL=FILE, got {spec!r}")
            label, _, path = spec.partition("=")
            per_target[label] = findings(load(pathlib.Path(path)), a.threshold)
    except RuntimeError as e:
        print(f"SCAN BROKEN: {e}", file=sys.stderr)
        pathlib.Path(a.out).write_text(
            "## The vulnerability scan could not run\n\n"
            f"```\n{e}\n```\n\n"
            "This is a failure of the **check**, not a clean result. While it "
            "persists nothing is reporting whether the published images are "
            "exposed, and a silent scan is worse than no scan because silence "
            "is read as 'nothing found'.\n")
        return 1

    total = sum(len(v) for v in per_target.values())

    # An image that couldn't be scanned takes priority over findings from the
    # ones that could. Otherwise a partly broken scan would be filed under
    # "vulnerabilities found", and the gap in coverage would go unmentioned
    # just because something else happened to be reported.
    if total == 0 and not a.partial:
        print(f"Scanned {len(per_target)} target(s); "
              f"nothing at {a.threshold} or above.")
        return 0

    if a.partial:
        lines = ["## The vulnerability scan was incomplete", "",
                 f"```\n{a.partial}\n```", "",
                 "**Some targets were not scanned, so this is not a clean "
                 "result for them.** Any findings below are from the targets "
                 "that could be scanned, and say nothing about the ones that "
                 "could not.", ""]
    else:
        lines = []
    if total == 0:
        lines += ["No findings at "
                  f"{a.threshold} or above among the targets that were scanned.", ""]
    else:
        lines += [f"## {total} vulnerability finding(s) at {a.threshold} or above", ""]
        lines += ["These are the **published** images, as a user pulls them today.", ""]
    for label in sorted(per_target):
        rows = per_target[label]
        if not rows:
            continue
        lines += [f"### `{label}`", "",
                  "| Severity | ID | Package | Installed | Fixed in |",
                  "|---|---|---|---|---|"]
        rows.sort(key=lambda r: (-ORDER.index(r["severity"]), r["id"]))
        for r in rows:
            lines.append(f"| {r['severity']} | {r['id']} | `{r['package']}` "
                         f"| {r['version']} | {r['fix']} |")
        lines.append("")

    clean = [k for k, v in per_target.items() if not v]
    if clean:
        lines += ["<details><summary>Targets with no findings "
                  f"({len(clean)})</summary>", ""]
        lines += [f"- `{k}`" for k in sorted(clean)]
        lines += ["", "</details>", ""]

    lines += [
        "---", "",
        "**What to do.** If the fix is in an Alpine package, the weekly "
        "rebuild will pick it up on its own; rerun the build workflow to get "
        "it sooner. If it is in Kea itself, that needs a version bump in "
        "`versions.json`, which is deliberately a human decision (D10).", "",
        "_Opened automatically by the vulnerability scan. The published SBOM "
        "carries a CPE for Kea, which is what lets this match Kea's own CVEs "
        "and not just Alpine's (D23)._",
    ]
    pathlib.Path(a.out).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if a.partial else 2


if __name__ == "__main__":
    sys.exit(main())
