#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Turn versions.json into the CI build matrix and tag lists.

The matrix is keyed on (branch, arch), NOT (branch, image, arch). That is
deliberate: every image in a branch is carved out of one builder stage, so a
job must build all of a branch's images together to compile Kea once. Fanning
out per image would recompile Kea for each one.

Usage:
    matrix.py matrix          GitHub Actions matrix for the build jobs
    matrix.py images  BRANCH  image suffixes for a branch
    matrix.py tags    BRANCH IMAGE [--registry REG --owner OWNER]
    matrix.py check           validate versions.json
"""
import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.json"
ARCHES = [
    {"arch": "amd64", "platform": "linux/amd64", "runner": "ubuntu-24.04"},
    {"arch": "arm64", "platform": "linux/arm64", "runner": "ubuntu-24.04-arm"},
]
DOCKERFILE = ROOT / "build" / "Dockerfile"
CONFIG_DIR = ROOT / "build" / "config"
STAGE = re.compile(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", re.M)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
DIGEST = re.compile(r"^alpine:[\w.]+@sha256:[0-9a-f]{64}$")


def load():
    with VERSIONS.open() as fh:
        return json.load(fh)["branches"]


def check():
    """Fail loudly on the mistakes that are easy to make by hand."""
    branches = load()
    errors = []
    for bid, b in branches.items():
        where = f"branches.{bid}"
        if not SEMVER.match(b.get("kea", "")):
            errors.append(f"{where}.kea: {b.get('kea')!r} is not X.Y.Z")
        elif not b["kea"].startswith(bid + "."):
            errors.append(f"{where}.kea: {b['kea']} does not belong to branch {bid}")
        # Even minor = stable, odd = development. We ship stable only.
        minor = int(bid.split(".")[1])
        if minor % 2 == 1:
            errors.append(f"{where}: minor {minor} is odd - that is a development branch")
        if not DIGEST.match(b.get("alpineRef", "")):
            errors.append(f"{where}.alpineRef: must be alpine:TAG@sha256:<64 hex>")
        if not b.get("images"):
            errors.append(f"{where}.images: empty")
        if "ctrl-agent" in b.get("images", []) and bid != "3.0":
            errors.append(f"{where}.images: kea-ctrl-agent was removed upstream after 3.0")
    # Every image named in versions.json must have a matching Dockerfile stage
    # and a config template. Without this, a typo or a forgotten stage only
    # surfaces after Kea has already compiled for 25 minutes in CI - which is
    # exactly how the missing ctrl-agent stage was found.
    stages = set(STAGE.findall(DOCKERFILE.read_text()))
    for bid, b in branches.items():
        for img in b.get("images", []):
            if img not in stages:
                errors.append(
                    f"branches.{bid}.images: no 'FROM ... AS {img}' stage in "
                    f"build/Dockerfile (have: {', '.join(sorted(stages))})"
                )
            # kea-tools ships no daemon config; everything else must have one.
            if img != "tools" and not (CONFIG_DIR / f"kea-{img}.conf").exists():
                errors.append(
                    f"branches.{bid}.images: missing build/config/kea-{img}.conf"
                )

    lts = [b for b in branches.values() if b.get("lts")]
    if len(lts) > 1:
        errors.append("more than one branch marked lts")
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"versions.json OK - {len(branches)} branches: {', '.join(branches)}")
    return 0


def tags(bid, image, registry, owner):
    b = load()[bid]
    names = [b["kea"], bid]
    if b.get("lts"):
        names.append(f"{bid}-lts")
    # No bare major tag and no "latest" - see docs/DECISIONS.md D10.
    return [f"{registry}/{owner}/kea-{image}:{t}" for t in names]


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("matrix")
    q = sub.add_parser("images"); q.add_argument("branch")
    t = sub.add_parser("tags")
    t.add_argument("branch"); t.add_argument("image")
    t.add_argument("--registry", default="ghcr.io"); t.add_argument("--owner", default="dapalab")
    a = p.parse_args()

    if a.cmd == "check":
        sys.exit(check())
    if a.cmd == "matrix":
        branches = load()
        include = [
            {
                "branch": bid,
                "kea": b["kea"],
                "alpineRef": b["alpineRef"],
                "images": " ".join(b["images"]),
                **arch,
            }
            for bid, b in branches.items()
            for arch in ARCHES
        ]
        print(json.dumps({"include": include}, separators=(",", ":")))
    elif a.cmd == "images":
        print(" ".join(load()[a.branch]["images"]))
    elif a.cmd == "tags":
        print("\n".join(tags(a.branch, a.image, a.registry, a.owner)))


if __name__ == "__main__":
    main()
