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
    matrix.py tags    BRANCH IMAGE [--date YYYYMMDD] [--registry REG --owner OWNER]
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
DATESTAMP = re.compile(r"^\d{8}$")
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

    # Tag composition. These are properties the published tags must have, not
    # restatements of what tags() does - a refactor that broke any of them
    # would publish a misleading tag, and a published tag cannot be recalled.
    for bid, b in branches.items():
        for img in b.get("images", []):
            names = [t.rsplit(":", 1)[1]
                     for t in tags(bid, img, "ghcr.io", "owner", date="20260101")]
            where = f"tags({bid}, {img})"
            if "latest" in names:
                errors.append(f"{where}: 'latest' must never be published (D10)")
            major = bid.split(".")[0]
            if major in names:
                errors.append(
                    f"{where}: bare major tag {major!r} must never be published - it "
                    f"would resolve to the shorter-lived stable branch (D10)")
            dated = [n for n in names if n.endswith("-20260101")]
            if len(dated) != 1:
                errors.append(
                    f"{where}: expected exactly one immutable date tag, got {dated}")
            elif dated[0] != f"{b['kea']}-20260101":
                errors.append(
                    f"{where}: date tag {dated[0]!r} should be {b['kea']}-20260101")
            if b["kea"] not in names or bid not in names:
                errors.append(f"{where}: missing {b['kea']!r} or {bid!r} in {names}")
            if len(names) != len(set(names)):
                errors.append(f"{where}: duplicate tags in {names}")
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"versions.json OK - {len(branches)} branches: {', '.join(branches)}")
    return 0


def tags(bid, image, registry, owner, date=None):
    """Tag list for one image on one branch.

    Exactly one tag here is immutable. The weekly rebuild re-pushes the same
    Kea versions against fresh Alpine packages, so EVERY version-shaped tag
    moves - including `3.2.0`, which reads like a pin and is not one. The
    date-suffixed tag `3.2.0-20260920` is never reused, so there is something
    to pin that actually holds still, and a cosign signature made against it
    keeps resolving to the bytes it was made over. See docs/DECISIONS.md D10.

    No bare major tag and no `latest`, unchanged: a bare `3` would resolve to
    the shorter-lived stable branch while the LTS outlives it.
    """
    b = load()[bid]
    names = []
    if date:
        # A date tag is permanent once pushed - it can never be corrected,
        # only abandoned. Validate it rather than discover the mistake in
        # the registry.
        if not DATESTAMP.match(date):
            raise ValueError(f"--date must be YYYYMMDD, got {date!r}")
        names.append(f"{b['kea']}-{date}")
    names += [b["kea"], bid]
    if b.get("lts"):
        names.append(f"{bid}-lts")
    return [f"{registry}/{owner}/kea-{image}:{t}" for t in names]


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("matrix")
    q = sub.add_parser("images"); q.add_argument("branch")
    t = sub.add_parser("tags")
    t.add_argument("branch"); t.add_argument("image")
    t.add_argument("--date", help="YYYYMMDD; adds the immutable date-suffixed tag")
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
        try:
            print("\n".join(tags(a.branch, a.image, a.registry, a.owner, a.date)))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
