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

    cosign finds a signature through a TAG named after its subject. GHCR has
    no Referrers API, so cosign's bundle format falls back to an index tagged
    `sha256-<hex>`, listing an untagged bundle manifest. (The older format
    tagged `sha256-<hex>.sig` directly; both are recognised.) See D25.

    A signature tag is only walked if its subject is kept. Deleting a
    superseded index without its signature would leave a tagged signature
    pointing at nothing, so a signature whose subject is being deleted goes
    with it - the tagged index and the bundle underneath.

THE TAG LIST HAS TO BE COMPLETE

    The registry pages tags/list. A tag the walk never sees cannot protect
    its digest, so a short list does not make this do less work - it makes
    it delete live images, and in amounts the fraction guard below does not
    catch. So every page is followed, and the result is checked against the
    tags GitHub's package API reports; if the two disagree, nothing is
    planned at all. See D26.

NOTHING YOUNGER THAN THE GRACE PERIOD

    Someone may have pinned a digest while it was still tagged. So a version
    younger than --min-age days (default 90) is kept even if nothing points
    at it any more - and it counts as a starting point for the walk, so
    whatever it refers to, and its signatures, are kept with it. See D17.

DRY RUN BY DEFAULT

    Deletions cannot be undone and the failure mode is every published image
    breaking at once. So this prints what it would do and changes nothing
    unless given --delete, and it refuses outright if its own safety checks
    do not hold.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

REGISTRY = "ghcr.io"
ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
SIG_TAG = re.compile(r"^sha256-([0-9a-f]{64})(?:\.(?:sig|att|sbom))?$")
# Refuse to delete more than this share of a package in one run. In steady
# state a build leaves four per-arch wrapper indexes per package among ~26
# new versions, so a correct run removes well under this; wanting to remove
# more means the reachability walk failed, not that there is a lot of
# garbage. --max-fraction raises it for one deliberate run (D26).
MAX_DELETE_FRACTION = 0.25
# Keep anything younger than this, reachable or not: a digest someone pinned
# while it was tagged should survive at least a quarter. D17.
MIN_AGE_DAYS = 90


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


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


LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')
MAX_TAG_PAGES = 1000


def tag_list(owner, pkg, token):
    """Every tag, following the registry's `Link: <...>; rel="next"` pages.

    Fails rather than returning a partial list: a Link header it cannot
    parse, a page that repeats a tag, or pages that never end.
    """
    url = f"https://{REGISTRY}/v2/{owner}/{pkg}/tags/list"
    tags, seen = [], set()
    for _ in range(MAX_TAG_PAGES):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as f:
            page = json.load(f).get("tags") or []
            link = f.headers.get("Link")
        again = seen.intersection(page)
        if again:
            raise RuntimeError(f"{pkg}: tags/list repeated {sorted(again)[0]!r} - "
                               "pagination is looping")
        seen.update(page)
        tags += page
        if not link:
            return tags
        m = LINK_NEXT.search(link)
        if not m:
            raise RuntimeError(f"{pkg}: cannot follow tags/list Link header {link!r}")
        # GHCR sends a path, not a URL.
        url = urllib.parse.urljoin(url, m.group(1))
    raise RuntimeError(f"{pkg}: tags/list still paging after {MAX_TAG_PAGES} pages")


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


def created(version):
    return datetime.datetime.fromisoformat(version["created_at"].replace("Z", "+00:00"))


def plan(owner, pkg, min_age_days=MIN_AGE_DAYS):
    token = registry_token(owner, pkg)
    tags = tag_list(owner, pkg, token)
    if not tags:
        raise RuntimeError(f"{pkg}: registry lists no tags at all - refusing")

    versions = gh_json(f"user/packages/container/{pkg}/versions?per_page=100")
    by_digest = {v["name"]: v for v in versions}

    # Two independent accounts of what is tagged. If they disagree, one of
    # them is incomplete, and that is exactly when nothing should be deleted.
    gh_tags = {t for v in versions for t in v["metadata"]["container"]["tags"]}
    if set(tags) != gh_tags:
        only_reg = sorted(set(tags) - gh_tags)
        only_gh = sorted(gh_tags - set(tags))
        raise RuntimeError(
            f"{pkg}: registry lists {len(set(tags))} tags, GitHub {len(gh_tags)} - refusing. "
            f"Only in registry: {only_reg[:3]}; only in GitHub: {only_gh[:3]}")

    # Signatures are walked only for subjects that survive; see the header.
    sigs = {t: "sha256:" + m.group(1) for t in tags if (m := SIG_TAG.match(t))}
    keep = reachable_from(owner, pkg, token, [t for t in tags if t not in sigs])
    # Young versions are roots too, not just exemptions: a young index may
    # point at older manifests, and those have to survive with it.
    cutoff = utcnow() - datetime.timedelta(days=min_age_days)
    young = [d for d, v in by_digest.items() if created(v) > cutoff]
    keep |= reachable_from(owner, pkg, token, young)
    keep |= reachable_from(owner, pkg, token, [t for t, subj in sigs.items() if subj in keep])

    doomed = {}
    for t, subj in sigs.items():
        if subj in keep:
            continue
        try:
            digest, _ = manifest(owner, pkg, token, t)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise RuntimeError(f"could not resolve {pkg}:{t}: {e}") from e
        doomed[digest] = subj

    delete = []
    for digest, v in by_digest.items():
        if digest in keep:
            continue
        if digest in doomed:
            delete.append((v, f"signature for {doomed[digest][:19]}… (being deleted)"))
            continue
        if v["metadata"]["container"]["tags"]:
            # Tagged but unreachable: the tag moved while this ran, most
            # likely. Never delete it.
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
    ap.add_argument("--min-age", type=int, default=MIN_AGE_DAYS, metavar="DAYS",
                    help=f"keep every version younger than this, reachable or "
                         f"not (default {MIN_AGE_DAYS}; 0 turns it off)")
    ap.add_argument("--max-fraction", type=float, default=MAX_DELETE_FRACTION,
                    metavar="F",
                    help=f"refuse to delete more than this share of a package "
                         f"(default {MAX_DELETE_FRACTION}; must be below 1)")
    a = ap.parse_args()
    if a.min_age < 0:
        ap.error(f"--min-age cannot be negative, got {a.min_age}")
    if not 0 < a.max_fraction < 1:
        # 1 or more is not a larger limit, it is no guard at all.
        ap.error(f"--max-fraction must be between 0 and 1, got {a.max_fraction}")

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
            by_digest, keep, delete = plan(a.owner, pkg, a.min_age)
        except (RuntimeError, urllib.error.URLError) as e:
            print(f"=== {pkg} ===\n  ERROR: {e}", file=sys.stderr)
            failures.append(pkg)
            continue

        print(f"=== {pkg} ===")
        print(f"  versions            : {len(by_digest)}")
        print(f"  reachable (keep)    : {len(by_digest) - len(delete)}")
        print(f"  to delete           : {len(delete)}")

        if delete and len(delete) / len(by_digest) > a.max_fraction:
            print(f"  REFUSING: would delete {len(delete)}/{len(by_digest)} "
                  f"(>{a.max_fraction:.0%}). The reachability walk is more "
                  f"likely broken than the package that full of garbage. "
                  f"If this plan is right, rerun with --max-fraction.",
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
