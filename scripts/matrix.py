#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Turn versions.json into the CI build matrix and tag lists.

The matrix is one job per (branch, arch), not per (branch, image, arch), on
purpose. Every image in a branch comes out of one builder stage, so a job
builds all of a branch's images together to compile Kea once. A job per image
would recompile Kea for each one.

Usage:
    matrix.py matrix          GitHub Actions matrix for the build jobs
    matrix.py images  BRANCH  image suffixes for a branch
    matrix.py tags    BRANCH IMAGE [--stamp YYYYMMDD-HHMM] [--registry REG --owner OWNER]
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
STAMP = re.compile(r"^\d{8}-\d{4}$")
DIGEST = re.compile(r"^alpine:[\w.]+@sha256:[0-9a-f]{64}$")


def load():
    with VERSIONS.open() as fh:
        return json.load(fh)["branches"]


def check():
    """Catch the mistakes that are easy to make when editing by hand."""
    branches = load()
    errors = []
    for bid, b in branches.items():
        where = f"branches.{bid}"
        if not SEMVER.match(b.get("kea", "")):
            errors.append(f"{where}.kea: {b.get('kea')!r} is not X.Y.Z")
        elif not b["kea"].startswith(bid + "."):
            errors.append(f"{where}.kea: {b['kea']} does not belong to branch {bid}")
        # Even minor versions are stable, odd ones development. We only
        # build stable ones.
        minor = int(bid.split(".")[1])
        if minor % 2 == 1:
            errors.append(f"{where}: minor {minor} is odd - that is a development branch")
        if not DIGEST.match(b.get("alpineRef", "")):
            errors.append(f"{where}.alpineRef: must be alpine:TAG@sha256:<64 hex>")
        if not b.get("images"):
            errors.append(f"{where}.images: empty")
        if "ctrl-agent" in b.get("images", []) and bid != "3.0":
            errors.append(f"{where}.images: kea-ctrl-agent was removed upstream after 3.0")
    # Every image named in versions.json needs a matching Dockerfile stage and
    # a config template. Without this check, a typo or a forgotten stage only
    # shows up after Kea has compiled for 25 minutes in CI, which is how the
    # missing ctrl-agent stage was first found.
    stages = set(STAGE.findall(DOCKERFILE.read_text()))
    for bid, b in branches.items():
        for img in b.get("images", []):
            if img not in stages:
                errors.append(
                    f"branches.{bid}.images: no 'FROM ... AS {img}' stage in "
                    f"build/Dockerfile (have: {', '.join(sorted(stages))})"
                )
            # kea-tools has no server config; everything else needs one.
            if img != "tools" and not (CONFIG_DIR / f"kea-{img}.conf").exists():
                errors.append(
                    f"branches.{bid}.images: missing build/config/kea-{img}.conf"
                )

    lts = [b for b in branches.values() if b.get("lts")]
    if len(lts) > 1:
        errors.append("more than one branch marked lts")

    # What the tag list must look like. These are rules the published tags
    # must follow, not a copy of what tags() does, so a change to tags() that
    # broke one would be caught. A published tag can't be taken back.
    for bid, b in branches.items():
        for img in b.get("images", []):
            names = [t.rsplit(":", 1)[1]
                     for t in tags(bid, img, "ghcr.io", "owner", stamp="20260101-0000")]
            where = f"tags({bid}, {img})"
            if "latest" in names:
                errors.append(f"{where}: 'latest' must never be published (D10)")
            major = bid.split(".")[0]
            if major in names:
                errors.append(
                    f"{where}: bare major tag {major!r} must never be published - it "
                    f"would resolve to the shorter-lived stable branch (D10)")
            dated = [n for n in names if n.endswith("-20260101-0000")]
            if len(dated) != 1:
                errors.append(
                    f"{where}: expected exactly one immutable tag, got {dated}")
            elif dated[0] != f"{b['kea']}-20260101-0000":
                errors.append(
                    f"{where}: immutable tag {dated[0]!r} should be "
                    f"{b['kea']}-20260101-0000")
            # fuse finds the stamped tag by matching the stamp, but a reader
            # would reasonably assume it's first too. Keep both true.
            elif names[0] != dated[0]:
                errors.append(
                    f"{where}: the immutable tag must come first, got {names}")
            if b["kea"] not in names or bid not in names:
                errors.append(f"{where}: missing {b['kea']!r} or {bid!r} in {names}")
            if len(names) != len(set(names)):
                errors.append(f"{where}: duplicate tags in {names}")

    # Check the stamp validator itself rejects bad stamps. Everything above
    # gives it a good stamp, so loosening STAMP back to date-only would go
    # unnoticed, and date-only is exactly the bug this format fixed: four
    # builds ran on 2026-09-20, which would have been one tag over four
    # different images.
    bid0 = next(iter(branches))
    img0 = branches[bid0]["images"][0]
    for bad in ("20260101", "2026-01-01", "20260101-000", "20260101-0000-1", ""):
        try:
            tags(bid0, img0, "ghcr.io", "owner", stamp=bad)
        except ValueError:
            continue
        if bad:  # an empty stamp legitimately means "no immutable tag"
            errors.append(
                f"stamp validator accepted {bad!r}; it must require YYYYMMDD-HHMM")
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"versions.json OK - {len(branches)} branches: {', '.join(branches)}")
    return 0


def tags(bid, image, registry, owner, stamp=None):
    """Tag list for one image on one branch. The immutable tag comes first.

    Exactly one tag here never changes. The weekly rebuild pushes the same
    Kea versions again with fresh Alpine packages, so every version-style tag
    moves, including `3.2.0`, which looks fixed but isn't. The stamped tag
    `3.2.0-20260920-2244` is never reused, so there's something to pin that
    stays put, and a cosign signature on those bytes can still be found by
    name after the moving tags have moved on.

    Why the time, not just the date: images rebuild more often than weekly.
    Every automerged Renovate update (Alpine digest, grouped actions) is a
    push to main, and so a build. Four builds ran on 2026-09-20 alone, so a
    date-only stamp would have published `3.2.0-20260920` four times with
    different contents.

    Minute precision is enough, because runs on main are queued one at a
    time (`concurrency: build-<ref>`) and a build takes 17+ minutes. Even so,
    fuse checks the tag doesn't already exist rather than relying on that.

    Still no bare major tag and no `latest`: a bare `3` would point at the
    shorter-lived stable branch, while the LTS lasts longer.

    See docs/DECISIONS.md D10, including why Renovate can't follow this tag
    and what automated setups should pin instead.
    """
    b = load()[bid]
    names = []
    if stamp:
        # Permanent once pushed: it can't be corrected, only abandoned. So
        # check it here, rather than finding the mistake in the registry.
        if not STAMP.match(stamp):
            raise ValueError(f"--stamp must be YYYYMMDD-HHMM, got {stamp!r}")
        names.append(f"{b['kea']}-{stamp}")
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
    t.add_argument("--stamp", help="YYYYMMDD-HHMM; adds the immutable stamped tag")
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
            print("\n".join(tags(a.branch, a.image, a.registry, a.owner, a.stamp)))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
