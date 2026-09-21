#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Tests for scripts/gc-packages.py - the only script here that destroys
published artifacts, and so the one that most needs to have failed for the
right reason in front of someone.

The fakes sit at the transport layer - urllib.request.urlopen for the
registry, subprocess.run for gh - rather than replacing tag_list() and
gh_json(). Stubbing those functions would test the planner against a tag
list that is complete by construction, and the pagination defect this suite
exists to catch lives inside tag_list() itself.

The fake registry paginates tags/list the way GHCR does, checked against the
live registry on 2026-09-20: `?n=5` returns five tags and

    Link: </v2/dapalab/kea-dhcp4/tags/list?last=3.0-lts&n=5>; rel="next"

A relative URL, in registry order (not sorted), resumed with `last=`.

Signatures are modelled as they are on the live registry (measured
2026-09-20: 60 of kea-dhcp4's 71 tags, none with a .sig suffix): an index
tagged `sha256-<hex>` listing an untagged Sigstore bundle manifest whose
`subject` is the signed digest. See D25.

The measure of every case is the DELETE calls the script actually issued,
not what it printed. A plan that looks right and a registry that is
destroyed are compatible; only the calls tell them apart.
"""
import contextlib
import datetime
import email.message
import hashlib
import io
import json
import pathlib
import subprocess
import sys
import traceback
import types
import urllib.error
import urllib.parse
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "gc-packages.py"
PKG = "kea-dhcp4"
OWNER = "dapalab"

# Every run happens at this moment, so ages are exact. Versions are OLD
# unless a case says otherwise - past the default grace period by a margin.
NOW = datetime.datetime(2026, 9, 21, 12, 0, tzinfo=datetime.timezone.utc)
OLD = 365

INDEX = "application/vnd.oci.image.index.v1+json"
IMAGE = "application/vnd.oci.image.manifest.v1+json"


def digest(name):
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


class Registry:
    """One model of the package, served both as a registry and as gh."""

    def __init__(self):
        self.manifests = {}      # digest -> manifest body
        self.tags = {}           # tag -> digest, in registry order
        self.page_size = 1000    # server-side cap on a tags/list page
        self.silent_cap = None   # truncate tags/list WITHOUT a Link header
        self.gh_page = 7         # versions per gh page, to exercise stitching
        self.unlisted = set()    # tags that resolve but tags/list omits
        self.loop_pages = False  # Link header points back at the first page
        self.bad_link = None     # a Link header that cannot be parsed
        self.gh_tag_on = {}      # tag -> digest GitHub reports it on, if different
        self.runs = []           # gh run list output
        self.run_list_fails = False
        self.deleted = []        # version ids gh was asked to DELETE
        self.ids = {}            # digest -> version id
        self.age = {}            # digest -> age in days (default OLD)

    # -- building fixtures -------------------------------------------------
    def _add(self, name, body):
        d = digest(name)
        self.manifests[d] = body
        self.ids.setdefault(d, 1000 + len(self.ids))
        return d

    def image(self, name):
        """A multi-arch index: two platform images and an attestation."""
        kids = [self._add(f"{name}/{k}", {"mediaType": IMAGE})
                for k in ("amd64", "arm64", "att")]
        idx = self._add(name, {"mediaType": INDEX,
                               "manifests": [{"digest": k} for k in kids]})
        return idx, kids

    def sign(self, subject, legacy=False):
        """cosign's bundle format on a registry without a Referrers API.
        Returns every version the signature adds: [tagged index, bundle]."""
        hexd = subject.split(":")[1]
        if legacy:
            sig = self._add(f"legacy-sig-of-{subject}", {"mediaType": IMAGE})
            self.tag(f"sha256-{hexd}.sig", sig)
            return [sig]
        bundle = self._add(f"bundle-of-{subject}", {
            "mediaType": IMAGE,
            "artifactType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "subject": {"digest": subject}})
        ref = self._add(f"referrers-of-{subject}", {
            "mediaType": INDEX,
            "manifests": [{"digest": bundle,
                           "artifactType": "application/vnd.oci.empty.v1+json"}]})
        self.tag(f"sha256-{hexd}", ref)
        return [ref, bundle]

    def tag(self, name, d):
        self.tags[name] = d

    def id(self, d):
        return self.ids[d]

    def versions(self):
        out = []
        for d in self.manifests:
            tags = [t for t, td in self.tags.items()
                    if self.gh_tag_on.get(t, td) == d]
            made = NOW - datetime.timedelta(days=self.age.get(d, OLD))
            out.append({"id": self.ids[d], "name": d,
                        "created_at": made.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "metadata": {"container": {"tags": tags}}})
        return out

    # -- the registry ------------------------------------------------------
    def urlopen(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        if u.netloc != "ghcr.io":
            raise AssertionError(f"unexpected host: {url}")
        if u.path == "/token":
            return Resp({"token": "t"})

        base = f"/v2/{OWNER}/{PKG}/"
        if not u.path.startswith(base):
            raise AssertionError(f"unexpected path: {url}")
        rest = u.path[len(base):]

        if rest == "tags/list":
            names = [t for t in self.tags if t not in self.unlisted]
            if self.silent_cap is not None:
                return Resp({"name": f"{OWNER}/{PKG}", "tags": names[:self.silent_cap]})
            if self.loop_pages and "last" in q:
                names = names[:1]
                return Resp({"name": f"{OWNER}/{PKG}", "tags": names},
                            {"Link": f'<{base}tags/list?last={names[0]}&n=1>; rel="next"'})
            if "last" in q:
                names = names[names.index(q["last"][0]) + 1:]
            n = min(int(q["n"][0]) if "n" in q else self.page_size, self.page_size)
            page, more = names[:n], len(names) > n
            hdrs = {}
            if more and self.bad_link:
                hdrs["Link"] = self.bad_link
            elif more:
                hdrs["Link"] = (f'<{base}tags/list?last={urllib.parse.quote(page[-1])}'
                                f'&n={n}>; rel="next"')
            return Resp({"name": f"{OWNER}/{PKG}", "tags": page}, hdrs)

        if rest.startswith("manifests/"):
            ref = rest[len("manifests/"):]
            d = self.tags.get(ref, ref)
            if d not in self.manifests:
                raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)
            return Resp(self.manifests[d], {"Docker-Content-Digest": d})

        raise AssertionError(f"unexpected registry call: {url}")

    # -- gh ----------------------------------------------------------------
    def run(self, cmd, **_):
        def done(out="", rc=0, err=""):
            return subprocess.CompletedProcess(cmd, rc, out, err)

        if cmd[:3] == ["gh", "run", "list"]:
            if self.run_list_fails:
                return done(rc=1, err="HTTP 502")
            return done(json.dumps([{"status": s, "databaseId": i}
                                    for i, s in enumerate(self.runs)]))
        if cmd[:3] == ["gh", "api", "-X"] and cmd[3] == "DELETE":
            self.deleted.append(int(cmd[4].rsplit("/", 1)[1]))
            return done()
        if cmd[:3] == ["gh", "api", "--paginate"]:
            vs = self.versions()
            # gh --paginate prints each page's array back to back: `[..][..]`
            pages = [vs[i:i + self.gh_page] for i in range(0, len(vs), self.gh_page)]
            return done("".join(json.dumps(p) for p in pages))
        raise AssertionError(f"unexpected command: {cmd}")


class Resp:
    def __init__(self, body, headers=None):
        self._body = json.dumps(body).encode()
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def load():
    """Compiled from source every time. importlib would reuse a cached .pyc
    whose recorded mtime (to the second) and size match - so an edit of the
    same length within the same second runs the OLD code. That made one
    mutation-test result a lie before this was fixed."""
    mod = types.ModuleType("gc_packages")
    mod.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), mod.__dict__)
    return mod


def run(reg, *args):
    """Run main() against the fakes. Returns (exit code, stdout, stderr)."""
    mod = load()
    mod.utcnow = lambda: NOW
    out, err = io.StringIO(), io.StringIO()
    argv = ["gc-packages.py", "--owner", OWNER, PKG, *args]
    with mock.patch.object(sys, "argv", argv), \
         mock.patch("urllib.request.urlopen", reg.urlopen), \
         mock.patch("subprocess.run", reg.run), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = mod.main()
        except SystemExit as e:   # argparse rejecting its arguments
            code = e.code
    return code, out.getvalue(), err.getvalue()


def fail_manifest(reg, ref):
    """Make one manifest fetch fail as a network error would."""
    real = reg.urlopen

    def flaky(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith(f"/manifests/{ref}"):
            raise urllib.error.URLError("connection reset")
        return real(req, timeout)
    reg.urlopen = flaky


def tag_list_error(reg):
    """tag_list() alone, so a guard is not credited with what a later one
    catches. Returns the RuntimeError text, or None if it returned a list."""
    mod = load()
    with mock.patch("urllib.request.urlopen", reg.urlopen):
        try:
            mod.tag_list(OWNER, PKG, "t")
        except RuntimeError as e:
            return str(e)
    return None


def healthy():
    """A package shaped like the real one: moving tags on the newest build,
    older builds held only by their stamped immutable tag, every index
    signed, and exactly one genuine orphan to collect."""
    reg = Registry()
    live, reg.sigs = [], []
    for i in range(6):
        idx, kids = reg.image(f"build-{i}")
        reg.tag(f"3.2.0-2026092{i}-0000", idx)
        # D25: the index and its two platform images, not the attestation.
        for subject in (idx, kids[0], kids[1]):
            reg.sigs += reg.sign(subject)
        live.append((idx, kids))
    newest = live[-1][0]
    for t in ("3.2.0", "3.2"):
        reg.tag(t, newest)
    # Superseded before stamped tags existed: no tag, no reachable parent.
    orphan, orphan_kids = reg.image("orphan")
    reg.orphan = [orphan, *orphan_kids]
    reg.live = live
    return reg


###############################################################################
# Cases
###############################################################################
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


@case
def paginated_tag_list_is_followed_to_the_end():
    """R2-1. The registry pages tags/list and says so with a Link header.
    Tags past the first page are the OLDER stamped builds - reachable only
    from those tags. Missing them does not make the collector do less; it
    makes it delete live images. The guard does not save it: the loss is
    well under half the package."""
    reg = healthy()
    reg.page_size = 4
    code, out, err = run(reg, "--delete")
    live = {reg.id(d) for idx, kids in reg.live for d in (idx, *kids)}
    hit = live & set(reg.deleted)
    must(not hit, f"deleted {len(hit)} LIVE versions (tags past page 1 never walked)")
    must(set(reg.deleted) == {reg.id(d) for d in reg.orphan},
         f"expected to delete exactly the orphan, deleted {sorted(reg.deleted)}")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def silently_truncated_tag_list_deletes_nothing():
    """R2-1. A tag list that is short WITHOUT saying so. Nothing at the call
    site can tell - only an independent count of what is tagged can. Two
    sources disagreeing about what is live is exactly when nothing should
    be deleted."""
    reg = healthy()
    # One old build's stamped tag missing: a loss well under the fraction
    # guard, so only the cross-check can catch it.
    reg.unlisted = {"3.2.0-20260920-0000"}
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], f"deleted {len(reg.deleted)} versions from a truncated tag list")
    must(code != 0, "exit 0 on a tag list that disagrees with gh - must refuse")
    must("GitHub" in err, f"refused, but not by the cross-check: {err.strip()[:120]}")


@case
def index_children_are_kept():
    """Untagged, but the platform images and attestation of a tagged index.
    'Delete untagged' would take these - see D17."""
    reg = healthy()
    code, out, err = run(reg, "--delete")
    kids = {reg.id(k) for _, ks in reg.live for k in ks}
    must(not kids & set(reg.deleted), "deleted a live index's children")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def signature_of_a_kept_subject_is_kept():
    """Both halves: the tagged referrers index and the bundle under it."""
    reg = healthy()
    code, out, err = run(reg, "--delete")
    hit = {reg.id(d) for d in reg.sigs} & set(reg.deleted)
    must(not hit, f"deleted {len(hit)} signature versions of live images")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def signature_of_a_deleted_subject_goes_with_it():
    """A signature is tagged, so reachability alone would keep it forever -
    pointing at nothing. The bundle under it goes too."""
    reg = healthy()
    sig = reg.sign(reg.orphan[0])
    code, out, err = run(reg, "--delete")
    want = {reg.id(d) for d in reg.orphan + sig}
    must(set(reg.deleted) == want,
         f"expected orphan + both signature versions {sorted(want)}, "
         f"deleted {sorted(reg.deleted)}")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def legacy_sig_suffix_is_still_recognised():
    """The older cosign format, `sha256-<hex>.sig`. None on the registry
    today; recognised anyway, so a format change cannot strand them."""
    reg = healthy()
    sig = reg.sign(reg.orphan[0], legacy=True)
    code, out, err = run(reg, "--delete")
    must(set(reg.deleted) == {reg.id(d) for d in reg.orphan + sig},
         f"legacy signature not collected with its subject: {sorted(reg.deleted)}")


@case
def tagged_but_unreachable_is_never_deleted():
    """Both sources agree on the tag NAMES, but the tag moved between the
    two reads: the registry resolves it to one build, GitHub still reports
    it on another. The build GitHub names is unreachable and tagged, and a
    tagged version is not deleted."""
    reg = healthy()
    stray, _ = reg.image("stray")
    reg.gh_tag_on = {"3.2": stray}
    code, out, err = run(reg, "--delete")
    must(reg.id(stray) not in reg.deleted, "deleted a TAGGED version")


@case
def looping_pagination_aborts():
    reg = healthy()
    reg.page_size = 4
    reg.loop_pages = True
    e = tag_list_error(reg)
    must(e and "looping" in e, f"tag_list did not detect the loop itself: {e}")
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], "deleted from a tag list that pages in a loop")
    must(code != 0, "exit 0 on looping pagination")


@case
def unparseable_link_header_aborts():
    """A Link header present but not understood is a truncated list."""
    reg = healthy()
    reg.page_size = 4
    reg.bad_link = "</v2/somewhere>; rel=prev"
    e = tag_list_error(reg)
    must(e and "cannot follow" in e, f"tag_list returned a partial list: {e}")
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], "deleted after ignoring a Link header")
    must(code != 0, "exit 0 with an unfollowed Link header")


@case
def unresolvable_signature_tag_aborts():
    """The old code skipped these silently. A signature tag that will not
    resolve for a doomed subject means the plan is incomplete."""
    reg = healthy()
    sig = reg.sign(reg.orphan[0])
    fail_manifest(reg, f"sha256-{reg.orphan[0].split(':')[1]}")
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], "deleted with an unresolved signature tag")
    must(code != 0, "exit 0 with an unresolved signature tag")


@case
def over_the_delete_fraction_aborts():
    """Most of the package unreachable means the walk broke, not that the
    package is full of garbage."""
    reg = healthy()
    limit = load().MAX_DELETE_FRACTION
    must(0 < limit < 1, f"MAX_DELETE_FRACTION = {limit} is not a guard")
    junk = len(reg.orphan)
    # Sized from the script's own limit, so the case stays meaningful if the
    # limit moves: just past it, not wildly past it.
    while junk / len(reg.manifests) <= limit:
        reg.image(f"junk-{junk}")
        junk += 4
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], f"deleted {len(reg.deleted)} versions past the fraction guard")
    must(code != 0, "exit 0 after refusing")
    must("REFUSING" in err, "no refusal message")


def over_default(reg):
    """Add junk until the plan is between the default limit and 0.5."""
    limit = load().MAX_DELETE_FRACTION
    must(limit < 0.45, f"default limit {limit} leaves no room for this case")
    junk = len(reg.orphan)
    while junk / len(reg.manifests) <= limit + 0.02:
        reg.image(f"junk-{junk}")
        junk += 4
    return {reg.id(d) for d in reg.manifests if d not in
            {x for idx, kids in reg.live for x in (idx, *kids)} | set(reg.sigs)}


@case
def override_allows_one_larger_cleanup():
    """The first real cleanup is a correct plan above the default limit.
    --max-fraction lets it through, and deletes exactly the garbage."""
    reg = healthy()
    want = over_default(reg)
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [] and code != 0, "default limit did not refuse")
    code, out, err = run(reg, "--delete", "--max-fraction", "0.5")
    must(set(reg.deleted) == want,
         f"override deleted {len(reg.deleted)}, expected exactly {len(want)} garbage")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def override_cannot_switch_the_guard_off():
    reg = healthy()
    over_default(reg)
    for bad in ("1", "1.5", "0", "-0.1"):
        code, out, err = run(reg, "--delete", "--max-fraction", bad)
        must(reg.deleted == [], f"--max-fraction {bad} deleted {len(reg.deleted)} versions")
        must(code != 0, f"--max-fraction {bad} was accepted")


@case
def young_orphan_is_kept():
    """D17's grace period: someone may have pinned it while it was tagged."""
    reg = healthy()
    for d in reg.orphan:
        reg.age[d] = 10
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], f"deleted {len(reg.deleted)} versions only 10 days old")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def grace_period_boundary_and_override():
    """89 days is inside the default grace period and 91 is past it, which
    pins the default at 90 - D17's promise. --min-age moves the line both
    ways, and 0 turns it off."""
    reg = healthy()
    for d in reg.orphan:
        reg.age[d] = 89
    run(reg, "--delete")
    must(reg.deleted == [], "an 89-day orphan was deleted: the default is under 90")
    reg = healthy()
    for d in reg.orphan:
        reg.age[d] = 91
    run(reg, "--delete")
    must(set(reg.deleted) == {reg.id(d) for d in reg.orphan},
         f"91-day orphan not collected at the default: {sorted(reg.deleted)}")
    reg = healthy()
    for d in reg.orphan:
        reg.age[d] = 91
    run(reg, "--delete", "--min-age", "120")
    must(reg.deleted == [], "--min-age 120 did not protect a 91-day orphan")
    reg = healthy()
    for d in reg.orphan:
        reg.age[d] = 1
    run(reg, "--delete", "--min-age", "0")
    must(set(reg.deleted) == {reg.id(d) for d in reg.orphan},
         "--min-age 0 did not turn the grace period off")


@case
def young_index_keeps_what_it_points_at():
    """A young index can point at OLDER manifests - identical bytes are
    reused across builds. Exempting only the young version itself would
    delete its contents out from under it."""
    reg = healthy()
    idx, kids = reg.orphan[0], reg.orphan[1:]
    reg.age[idx] = 5          # the children stay OLD
    code, out, err = run(reg, "--delete")
    hit = {reg.id(k) for k in kids} & set(reg.deleted)
    must(not hit, f"deleted {len(hit)} old children of a young index")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def young_image_keeps_its_signature():
    reg = healthy()
    sig = reg.sign(reg.orphan[0])
    reg.age[reg.orphan[0]] = 5
    code, out, err = run(reg, "--delete")
    hit = {reg.id(d) for d in sig} & set(reg.deleted)
    must(not hit, "deleted the signature of an image inside the grace period")


@case
def negative_min_age_is_rejected():
    reg = healthy()
    code, out, err = run(reg, "--delete", "--min-age", "-1")
    must(reg.deleted == [] and code != 0, "--min-age -1 was accepted")


@case
def dry_run_is_the_default():
    """'Dry run by default' is a claim. Claims in this repo get tested."""
    reg = healthy()
    code, out, err = run(reg)
    must(reg.deleted == [], f"deleted {len(reg.deleted)} versions without --delete")
    must("DRY RUN" in out, "did not say it was a dry run")
    must(code == 0, f"exit {code}: {err.strip()}")


@case
def refuses_while_a_build_runs():
    reg = healthy()
    reg.runs = ["completed", "in_progress"]
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], "deleted during a live build")
    must(code != 0, "exit 0 while a build was running")


@case
def refuses_when_it_cannot_tell_if_a_build_runs():
    reg = healthy()
    reg.run_list_fails = True
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], "deleted without knowing whether a build was running")
    must(code != 0, "exit 0 when gh run list failed")


@case
def unresolvable_tag_aborts():
    """A tag whose manifest will not resolve leaves reachability unknown.
    Both sources agree the tag exists, so only the walk can notice."""
    reg = healthy()
    fail_manifest(reg, "3.2.0-20260920-0000")
    code, out, err = run(reg, "--delete")
    must(reg.deleted == [], "deleted with an unresolvable tag in the walk")
    must(code != 0, "exit 0 with an unresolvable tag")


def main():
    failed = 0
    for fn in CASES:
        label = fn.__name__.replace("_", " ")
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  \033[31mFAIL\033[0m  {label} -- {e}")
        except Exception:
            failed += 1
            print(f"  \033[31mFAIL\033[0m  {label} -- crashed:")
            traceback.print_exc()
        else:
            print(f"  \033[32mPASS\033[0m  {label}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
