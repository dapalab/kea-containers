#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Validate the SPDX document an image ships, against the version it claims.

Reads the document on stdin; argv[1] is the Kea version the image should be.
A separate file rather than an inline `python3 -c` inside the shell function:
the inline version needed nested quote escaping, got it wrong, and the error
was invisible because stderr was discarded.
"""
import json
import sys


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    if not want:
        print("no expected version given", file=sys.stderr)
        return 1
    try:
        doc = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"not valid JSON: {e}", file=sys.stderr)
        return 1

    pkgs = [p for p in doc.get("packages", []) if p.get("name") == "kea"]
    if len(pkgs) != 1:
        print(f"expected exactly one 'kea' package, found {len(pkgs)}", file=sys.stderr)
        return 1
    p = pkgs[0]

    if p.get("versionInfo") != want:
        print(f"document says kea {p.get('versionInfo')!r}, image is {want!r}",
              file=sys.stderr)
        return 1
    if p.get("licenseDeclared") != "MPL-2.0":
        print(f"licenseDeclared is {p.get('licenseDeclared')!r}, expected MPL-2.0",
              file=sys.stderr)
        return 1
    sha = (p.get("checksums") or [{}])[0].get("checksumValue", "")
    if len(sha) != 64:
        print(f"source checksum missing or malformed: {sha!r}", file=sys.stderr)
        return 1
    locators = [r.get("referenceLocator", "") for r in p.get("externalRefs", [])]
    if not any(l.startswith(f"cpe:2.3:a:isc:kea:{want}") for l in locators):
        print(f"no CPE for kea {want}; a scanner cannot match Kea CVEs without one",
              file=sys.stderr)
        return 1

    print(f"kea {want}, sha256:{sha[:12]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
