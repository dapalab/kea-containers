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
# Kubernetes examples carry the Kea config inline in a ConfigMap, so it is
# not a .conf file and would otherwise go unchecked - which is exactly the
# sort of file nobody notices is broken until they apply it.
YAML_PATTERNS = ["examples/kubernetes/*.yaml"]
ROOT = pathlib.Path(__file__).resolve().parent.parent

# Strip // line comments, but not inside a string (e.g. an "http://..." value).
LINE_COMMENT = re.compile(r'(?<!:)//.*$', re.M)
BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.S)
# `    kea-dhcp4.conf: |` followed by a more-indented block. Parsed by hand
# rather than with PyYAML so this keeps running on any stock python3.
EMBEDDED = re.compile(r'^([ \t]*)([\w.-]+\.conf):[ \t]*\|[ \t]*$', re.M)


def strip(text: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def embedded_configs(text: str):
    """Yield (name, config) for each `<name>.conf: |` block in a YAML file."""
    lines = text.splitlines()
    for m in EMBEDDED.finditer(text):
        indent, name = m.group(1), m.group(2)
        start = text[:m.start()].count("\n") + 1
        body, want = [], len(indent)
        for line in lines[start:]:
            if line.strip() and (len(line) - len(line.lstrip())) <= want:
                break
            body.append(line[want + 2:] if len(line) > want else "")
        yield name, "\n".join(body)


def main() -> int:
    files = sorted(p for pat in PATTERNS for p in ROOT.glob(pat))
    yamls = sorted(p for pat in YAML_PATTERNS for p in ROOT.glob(pat))
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

    for y in yamls:
        rel = y.relative_to(ROOT)
        found = 0
        for name, cfg in embedded_configs(y.read_text()):
            found += 1
            try:
                json.loads(strip(cfg))
            except json.JSONDecodeError as e:
                print(f"  FAIL  {rel} [{name}]: {e}", file=sys.stderr)
                rc = 1
            else:
                print(f"  ok    {rel} [{name}]")
        # Silently finding nothing would mean a renamed key quietly disables
        # this check - the failure mode being guarded against everywhere else
        # in this repo.
        if not found:
            print(f"  FAIL  {rel}: no embedded '*.conf: |' block found; did the "
                  f"key change?", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
