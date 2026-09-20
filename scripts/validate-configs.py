#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Check that every shipped config parses as JSON once comments are stripped.

Kea accepts // and /* */ comments, so these files are not valid JSON as-is.
This catches the mistakes a human makes editing them - a trailing comma, an
unbalanced brace - without needing Kea itself, so it runs in the lint job in
seconds rather than waiting on a 30-minute build.

Note this proves the file is well-formed, NOT that Kea accepts it. The build
job runs `kea-dhcpX -t` against the real binary for that.
"""
import json
import re
import sys
import pathlib

PATTERNS = ["build/config/*.conf", "examples/*.conf", "test/*.conf"]
ROOT = pathlib.Path(__file__).resolve().parent.parent

# Strip // line comments, but not inside a string (e.g. an "http://..." value).
LINE_COMMENT = re.compile(r'(?<!:)//.*$', re.M)
BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.S)


def strip(text: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def main() -> int:
    files = sorted(p for pat in PATTERNS for p in ROOT.glob(pat))
    if not files:
        print("error: no config files found", file=sys.stderr)
        return 1
    rc = 0
    for f in files:
        rel = f.relative_to(ROOT)
        try:
            json.loads(strip(f.read_text()))
        except json.JSONDecodeError as e:
            print(f"  FAIL  {rel}: {e}", file=sys.stderr)
            rc = 1
        else:
            print(f"  ok    {rel}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
