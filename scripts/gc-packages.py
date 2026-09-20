#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Delete unreachable GHCR package versions - safely.

WHY THIS IS NOT "DELETE UNTAGGED"

    Every off-the-shelf GHCR cleanup action offers "delete untagged versions"
    and it would destroy this registry. A multi-arch image is an INDEX; the
    per-architecture manifests underneath it, and the SBOM/provenance
    attestation manifests beside them, are all untagged package versions that
    the index points at.

    Measured on 2026-09-20, before any cleanup existed:

        kea-dhcp4: 136 versions, 94 untagged
                   48 of those 94 were live children of a working tag

    Roughly half. Deleting "untagged" would have broken every published
    image. See docs/DECISIONS.md D17.

WHAT THIS DOES INSTEAD

    Computes reachability. A version is KEPT if it is:

      - tagged, or
      - referenced by the index of anything that is kept, or
      - a cosign signature / attestation for something that is kept.

    Everything else is genuinely orphaned: the remains of an earlier build
    whose tags have since moved on. Those are what it deletes.

SIGNATURES ARE PAIRED WITH WHAT THEY SIGN

    cosign stores a signature as a TAG of the form `sha256-<digest>.sig`.
    Deleting a superseded index without its signature leaves a tagged
    signature pointing at nothing - so a `.sig` whose subject is being
    deleted is deleted with it, and a `.sig` whose subject is being kept is
    itself kept.

DRY RUN BY DEFAULT

    Deletions cannot be undone and the failure mode is every published image
    breaking at once. So this prints what it would do and changes nothing
    unless given --delete, and it refuses outright if its own safety checks
    do not hold.
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

REGISTRY = "ghcr.io"
ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
SIG_TAG = re.compile(r"^sha256-([0-9a-f]{64})\.(sig|att|sbom)$")
# Refuse to delete more than this share of a package in one run. A correct
# run removes a handful of superseded builds; wanting to remove most of the
# package means the reachability walk failed, not that there is a lot of
# garbage.
MAX_DELETE_FRACTION = 0.5


def build_in_flight():
    """True if a build workflow is running right now.

    Garbage collection races a build badly: `fuse` pushes an index and then
    applies tags to it, so a walk that lands in between sees a real, live
    index as unreachable and proposes deleting it. The window is small and
    the consequence is not, so refuse rather than hope.
    """
    r = subprocess.run(
        ["gh", "run", "list", "--workflow", "build.yml", "--limit", "5",
         "--json", "status,databaseId"],
        capture_output=True, text=True)
    if r.returncode != 0:
        # Cannot tell - treat as in flight. Failing closed costs a rerun;
        # failing open can cost the registry.
        raise RuntimeError(f"could not check for running builds: {r.stderr.strip()}")
    runs = json.loads(r.stdout or "[]")
    live = [x["databaseId"] for x in runs
            if x["status"] in ("queued", "in_progress", "waiting", "requested", "pending")]
    return live


def gh_json(path):
    r = subprocess.run(["gh", "api", "--paginate", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {r.stderr.strip()}")
    # --paginate concatenates JSON arrays as "][", stitch them back together
    return json.loads(r.stdout.replace("][", ","))


def registry_token(owner, pkg):
    url = f"https://{REGISTRY}/token?scope=repository:{owner}/{pkg}:pull"
    with urllib.request.urlopen(url, timeout=30) as f:
        return json.load(f)["token"]


def manifest(owner, pkg, token, ref):
    req = urllib.request.Request(
        f"https://{REGISTRY}/v2/{owner}/{pkg}/manifests/{ref}",
        headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT})
    with urllib.request.urlopen(req, timeout=30) as f:
        return f.headers.get("Docker-Content-Digest"), json.load(f)


def tag_list(owner, pkg, token):
    req = urllib.request.Request(
        f"https://{REGISTRY}/v2/{owner}/{pkg}/tags/list",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as f:
        return json.load(f).get("tags") or []


def reachable_from(owner, pkg, token, tags):
    """Digests reachable by walking every tag, indexes included."""
    found, seen = set(), set()

    def walk(ref):
        if ref in seen:
            return
        seen.add(ref)
        try:
            digest, man = manifest(owner, pkg, token, ref)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise RuntimeError(f"could not resolve {pkg}:{ref}: {e}") from e
        if digest:
            found.add(digest)
        for child in man.get("manifests", []):
            found.add(child["digest"])
            walk(child["digest"])

    for t in tags:
        walk(t)
    return found


def plan(owner, pkg):
    token = registry_token(owner, pkg)
    tags = tag_list(owner, pkg, token)
    if not tags:
        raise RuntimeError(f"{pkg}: registry lists no tags at all - refusing")

    keep = reachable_from(owner, pkg, token, tags)

    versions = gh_json(f"user/packages/container/{pkg}/versions?per_page=100")
    by_digest = {v["name"]: v for v in versions}

    # A signature tag names its subject in the tag itself. Keep it if the
    # subject survives; delete it with the subject if not.
    sig_of = {}
    for t in tags:
        m = SIG_TAG.match(t)
        if m:
            subject = "sha256:" + m.group(1)
            try:
                digest, _ = manifest(owner, pkg, token, t)
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue
            if digest:
                sig_of[digest] = subject

    delete = []
    for digest, v in by_digest.items():
        if digest in keep and digest not in sig_of:
            continue
        if digest in sig_of:
            # Tagged signature: its fate follows its subject.
            if sig_of[digest] in keep:
                continue
            delete.append((v, f"signature for {sig_of[digest][:19]}… (being deleted)"))
            continue
        tags_on = v["metadata"]["container"]["tags"]
        if tags_on:
            # Tagged but unreachable should be impossible; never delete it.
            continue
        delete.append((v, "unreachable from any tag"))

    return by_digest, keep, delete


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner", default="dapalab")
    ap.add_argument("packages", nargs="+", help="package names, e.g. kea-dhcp4")
    ap.add_argument("--delete", action="store_true",
                    help="actually delete (default: dry run, changes nothing)")
    a = ap.parse_args()

    try:
        live_runs = build_in_flight()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if live_runs:
        ids = ", ".join(str(i) for i in live_runs)
        print(f"ERROR: build workflow is running ({ids}).", file=sys.stderr)
        print("  fuse pushes an index and then tags it; a walk landing in "
              "between would see a live index as unreachable.", file=sys.stderr)
        print("  Wait for it to finish and run again.", file=sys.stderr)
        return 1

    total_del = 0
    failures = []
    for pkg in a.packages:
        try:
            by_digest, keep, delete = plan(a.owner, pkg)
        except (RuntimeError, urllib.error.URLError) as e:
            print(f"=== {pkg} ===\n  ERROR: {e}", file=sys.stderr)
            failures.append(pkg)
            continue

        print(f"=== {pkg} ===")
        print(f"  versions            : {len(by_digest)}")
        print(f"  reachable (keep)    : {len(by_digest) - len(delete)}")
        print(f"  to delete           : {len(delete)}")

        if delete and len(delete) / len(by_digest) > MAX_DELETE_FRACTION:
            print(f"  REFUSING: would delete {len(delete)}/{len(by_digest)} "
                  f"(>{MAX_DELETE_FRACTION:.0%}). The reachability walk is more "
                  f"likely broken than the package that full of garbage.",
                  file=sys.stderr)
            failures.append(pkg)
            continue

        for v, why in delete:
            print(f"    - {v['name'][:26]}…  id={v['id']}  {why}")
        if not a.delete:
            print("  DRY RUN - nothing deleted. Pass --delete to apply.")
            continue
        for v, _ in delete:
            r = subprocess.run(
                ["gh", "api", "-X", "DELETE",
                 f"user/packages/container/{pkg}/versions/{v['id']}"],
                capture_output=True, text=True)
            if r.returncode != 0:
                print(f"    FAILED to delete {v['id']}: {r.stderr.strip()}",
                      file=sys.stderr)
                failures.append(pkg)
            else:
                total_del += 1
        print(f"  deleted {len(delete)}")

    if failures:
        print(f"\nfailed for: {', '.join(sorted(set(failures)))}", file=sys.stderr)
        return 1
    if a.delete:
        print(f"\nDeleted {total_del} versions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
