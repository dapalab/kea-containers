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
            # fuse selects the immutable tag by matching the stamp, but a
            # reader will reasonably assume position too. Keep both true.
            elif names[0] != dated[0]:
                errors.append(
                    f"{where}: the immutable tag must come first, got {names}")
            if b["kea"] not in names or bid not in names:
                errors.append(f"{where}: missing {b['kea']!r} or {bid!r} in {names}")
            if len(names) != len(set(names)):
                errors.append(f"{where}: duplicate tags in {names}")

    # Negative control on the stamp validator itself. Everything above feeds
    # it a well-formed stamp, so loosening STAMP back to date-only would go
    # unnoticed - and date-only is the exact bug this format replaced: four
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

    Exactly one tag here is immutable. The weekly rebuild re-pushes the same
    Kea versions against fresh Alpine packages, so EVERY version-shaped tag
    moves - including `3.2.0`, which reads like a pin and is not one. The
    stamped tag `3.2.0-20260920-2203` is never reused, so there is something
    to pin that holds still, and a cosign signature made over those bytes
    keeps resolving by name after the moving tags have left them behind.

    WHY THE TIME, NOT JUST THE DATE. Images rebuild more often than weekly:
    every automerged Renovate PR (Alpine digest, grouped actions) is a push
    to main and therefore a build. Four builds ran on 2026-09-20 alone. A
    date-only stamp would have published `3.2.0-20260920` four times over
    different bytes, which is the opposite of immutable.

    Minute resolution suffices because `concurrency: build-<ref>` serialises
    runs on main and a build takes 17+ minutes - but fuse asserts the tag
    does not already exist rather than relying on that argument.

    No bare major tag and no `latest`, unchanged: a bare `3` would resolve to
    the shorter-lived stable branch while the LTS outlives it.

    See docs/DECISIONS.md D10, including why this tag is deliberately
    invisible to Renovate and what its users should pin instead.
    """
    b = load()[bid]
    names = []
    if stamp:
        # Permanent once pushed: it can never be corrected, only abandoned.
        # Validate here rather than discover the mistake in the registry.
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
