#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Assert build.yml's path filters and build-skip.yml's are exact inverses.

WHY THIS EXISTS
    build.yml skips the image matrix for changes that cannot affect an image.
    Branch protection requires a check named 'required', which build.yml
    normally provides - so for the skipped cases build-skip.yml provides it
    instead, using the INVERSE path filter.

    If those lists ever drift, the consequence is not a build failure. It is a
    pull request that can never merge: neither workflow triggers, 'required'
    never reports, and branch protection waits forever. Or, less dangerously,
    both trigger and report the same check twice.

    That is a silent, confusing failure discovered at the worst moment. This
    turns it into a lint error found in seconds.

    build.yml carries the filter on BOTH its push and pull_request triggers,
    so all three lists must agree.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / ".github/workflows/build.yml"
SKIP = ROOT / ".github/workflows/build-skip.yml"

# A filter block: the key, then comment or '- entry' lines.
BLOCK = r"{key}:\n((?:[ \t]*#.*\n|[ \t]*-[ \t]*'[^']+'\n)+)"
ENTRY = re.compile(r"-\s*'([^']+)'")


def blocks(path: pathlib.Path, key: str) -> list[list[str]]:
    text = path.read_text()
    return [sorted(ENTRY.findall(b))
            for b in re.findall(BLOCK.format(key=key), text)]


def main() -> int:
    for f in (BUILD, SKIP):
        if not f.exists():
            print(f"error: {f.relative_to(ROOT)} is missing. build.yml and "
                  f"build-skip.yml are a pair: deleting one strands every pull "
                  f"request the other was covering.", file=sys.stderr)
            return 1

    ignore = blocks(BUILD, "paths-ignore")
    skip = blocks(SKIP, "paths")

    if len(ignore) != 2:
        print(f"error: expected 2 paths-ignore blocks in {BUILD.name} "
              f"(push and pull_request), found {len(ignore)}", file=sys.stderr)
        return 1
    if len(skip) != 1:
        print(f"error: expected 1 paths block in {SKIP.name}, found {len(skip)}",
              file=sys.stderr)
        return 1

    expected = skip[0]
    rc = 0
    for i, got in enumerate(ignore):
        where = ["push", "pull_request"][i]
        if got != expected:
            rc = 1
            print(f"error: {BUILD.name} {where} paths-ignore does not match "
                  f"{SKIP.name} paths", file=sys.stderr)
            for extra in sorted(set(got) - set(expected)):
                print(f"         only in {BUILD.name}: {extra}", file=sys.stderr)
            for missing in sorted(set(expected) - set(got)):
                print(f"         only in {SKIP.name}:  {missing}", file=sys.stderr)

    if rc == 0:
        print(f"workflow path filters agree ({len(expected)} entries, "
              f"3 lists checked)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
