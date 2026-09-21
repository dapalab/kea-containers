# Behind the scenes

This is the project's notebook: every significant design decision, why it
was made, what else was considered, and what we measured along the way. It's
written for anyone curious about how the images are put together, and for
future maintainers (including future us) wondering "why is it done like
this?"

Some entries have **amendments** added later. When we found that an earlier
decision was wrong, or was right for the wrong reason, we left the original
reasoning in place and added what we learned underneath, so the history stays
honest.

Upstream facts were checked on **2026-09-19** unless an entry says otherwise.

## Contents

| | |
|---|---|
| [D1](#d1--build-kea-from-source) | Build Kea from source |
| [D2](#d2--check-the-source-with-iscs-pgp-signature) | Check the source with ISC's PGP signature |
| [D3](#d3--one-image-per-server) | One image per server |
| [D4](#d4--one-repository-for-all-the-images) | One repository for all the images |
| [D5](#d5--a-separate-kea-tools-image) | A separate `kea-tools` image |
| [D6](#d6--each-branch-lists-its-own-images) | Each branch lists its own images |
| [D7](#d7--switch-every-optional-feature-on-or-off-explicitly) | Switch every optional feature on or off explicitly |
| [D8](#d8--non-root-with-file-capabilities) | Non-root, with file capabilities |
| [D9](#d9--a-commented-template-as-the-default-config) | A commented template as the default config |
| [D10](#d10--tags) | Tags |
| [D11](#d11--native-arm64-builds-no-emulation) | Native arm64 builds, no emulation |
| [D12](#d12--licence-mpl-20) | Licence: MPL-2.0 |
| [D13](#d13--mysql-and-postgresql-support) | MySQL and PostgreSQL support |
| [Phase 1](#phase-1-results) | Phase 1 results |
| [D14](#d14--install_umask0022) | `install_umask=0022` |
| [D15](#d15--oci-image-labels) | OCI image labels |
| [D16](#d16--renovate-self-hosted-as-a-github-app) | Renovate, self-hosted as a GitHub App |
| [D17](#d17--cleaning-up-old-package-versions-safely) | Cleaning up old package versions safely |
| [D18](#d18--keyless-signing) | Keyless signing |
| [D19](#d19--no-automated-dhcpv6-exchange-test) | No automated DHCPv6 exchange test |
| [D20](#d20--branch-protection) | Branch protection |
| [D21](#d21--prove-the-published-image-is-the-tested-one) | Prove the published image is the tested one |
| [D22](#d22--test-that-kea-lfc-really-compacts-the-lease-file) | Test that `kea-lfc` really compacts the lease file |
| [D23](#d23--put-kea-in-its-own-sbom) | Put Kea in its own SBOM |
| [D24](#d24--daily-vulnerability-scans-with-grype) | Daily vulnerability scans, with grype |
| [D25](#d25--what-gets-signed) | What gets signed |
| [D26](#d26--making-the-package-collector-trustworthy) | Making the package collector trustworthy |
| [D27](#d27--check-every-image-before-tagging-any) | Check every image before tagging any |

### A lesson that runs through all of this

The same kind of bug turned up again and again: **a check that couldn't
fail.** A test that passed no matter what, a verification whose exit code was
ignored, a guard that another guard always caught first. Each looked fine,
because it had only ever been run against working code.

So the rule we settled on is: **a check isn't finished until we've seen it
fail for the right reason.** In practice that means deliberately breaking the
thing it protects and watching it go red. You'll see that idea in most of the
entries below.

---

## D1 — Build Kea from source

**Decision.** Compile Kea from ISC's source tarball.

**Why.** ISC's own binary packages (apk on Cloudsmith, deb and rpm) are only
built for x86_64. Building from source is the only way to get arm64, and it's
the reason this project exists.

**Considered: Alpine's own `kea` package.** It doesn't exist. We checked
Alpine's package index (`APKINDEX`) directly for `edge/community` and `v3.24`,
on both x86_64 and aarch64: 22,912 packages, none named `kea`. The closest
match, `kealib`, is an unrelated library for satellite imagery. So there's no
ready-made package to use on any architecture.

---

## D2 — Check the source with ISC's PGP signature

**Decision.** Check the tarball's detached `.asc` signature against a copy of
ISC's code-signing keys kept in this repository. Don't record a SHA-256 hash
in `versions.json`.

**Why a signature and not a hash.** ISC doesn't publish checksum files. We
checked: `.sha256`, `.sha512`, `SHA256SUMS` and `CHECKSUMS` all return 404,
and the download directory holds only `kea-X.Y.Z.tar.xz` and its `.asc`.

So any hash would be one we'd written down ourselves. Renovate can't compute
a new one when it bumps a version, so every bump would leave a stale hash and
fail the build until someone updated it by hand. A signature is stronger
anyway: it proves ISC signed these exact bytes, not just that they match
something we saw once.

**Why the keys are stored here.** The build creates its keyring from the
stored keys *only*, so a tarball signed by any other key fails with "No
public key" and the build stops. If the build downloaded the keys instead,
someone able to tamper with downloads could supply both a tarball and a key
to match it.

**Something to expect.** The keyblock holds **seven** individual ISC
engineers' keys, and different releases are signed by different people (3.2.0
by Andrei Pavel, 3.0.4 by Wlodek Wencel), so we can't pin a single
fingerprint. If ISC adds an eighth signer, builds will fail until someone
reviews the new key and updates the stored block. That's intentional: trusting
a new key should be a deliberate step.

### Amendment, 2026-09-20: gpg's exit code isn't enough

The reasoning above holds for the case it considered: a tarball signed by a
key outside the block really does fail (exit 2). But the check relied on
`gpg`'s exit code, and that turned out to be a mistake.

**`gpg --batch --verify` exits 0 even when the signing key has expired or been
revoked.** We reproduced both with GnuPG 2.4.4:

| Case | What gpg prints | Exit | Status token |
|---|---|---|---|
| valid | `Good signature from ...` | 0 | `GOODSIG` |
| expired key | `Good signature ... [expired]` | **0** | `EXPKEYSIG` |
| revoked key | `Good signature ...` plus a warning | **0** | `REVKEYSIG` |
| bad signature | `BAD signature` | 1 | `BADSIG` |
| signer not in block | `Can't check signature` | 2 | `NO_PUBKEY` |

The revoked case is the worrying one. gpg still prints **"Good signature"**,
with only a warning line to tell the difference, so the build would have kept
passing.

In every bad case `GOODSIG` is simply missing. So the check now looks for
`GOODSIG` being **present**, rather than for known-bad tokens being absent.
That way, any new failure mode GnuPG adds in future will also fail the check,
with no changes needed.

**A premise we had to correct.** The obvious worry, "ISC's keys will expire
one day", doesn't apply: all seven keys are set to **never expire** (checked
2026-09-20). The real risk is *revocation*, in this sequence:

1. An ISC engineer's key is compromised and ISC revokes it.
2. We update the stored keyblock for an unrelated reason, say an eighth
   signer.
3. The new block now includes the revocation.
4. Under the old check, a tarball signed with the compromised key would still
   have passed, printing "Good signature".

Updating the keyblock is exactly when we'd *learn* about a revocation, and
exactly when the old check would have started ignoring it.

**Why the check moved out of the Dockerfile.** It now lives in
`build/verify-tarball.sh`. An inline `RUN` line can't be tested, whereas
`test/verify-signature-test.sh` runs the real script against good, expired,
revoked, untrusted-signer and tampered fixtures. We confirmed those tests fail
against the old logic, which accepted the expired and revoked cases, before
trusting them. They need only `gpg`, not a Kea compile, so they run in `lint`
in seconds.

**Supporting changes.**

- `build/keys/README.md` records the seven fingerprints, the date, and how
  they were checked: the stored file is byte-identical to
  `www.isc.org/docs/isc-keyblock.asc`, which is served by a different host
  from the tarballs.
- `scripts/check-upstream.py` now checks the keyblock weekly. If a key is
  revoked, expired or about to expire, it exits with code 3 and opens an issue
  with its own title.
- Since no real key expires, that warning could only ever have said
  "healthy", so the test suite also drives it with revoked and expiring
  fixtures.

### Amendment, 2026-09-20: removing a `sha256sum` that checked nothing

The verification step used to end with:

```dockerfile
sha256sum kea.tar.xz; \
```

It printed a hash and compared it with nothing, so it could never fail. In
the most security-sensitive part of the build, it made it look as though
there were two independent checks when there was one.

The main reasoning above still stands: a valid signature over these exact
bytes is stronger than a hash from the same server. So this was about being
honest, not about cryptography.

We checked for an alternative. If ISC published a hash alongside the `.asc`,
we could fetch and compare it. Re-checked 2026-09-20: `.sha256`, `.sha512`
and `.SHA256` all return 404 for both 3.2.0 and 3.0.4. So we removed the
line, and a comment in the Dockerfile now says the missing hash check is
deliberate.

The rule it leaves behind: nothing in that step should look like a check
without being one. (The hash did find an honest use later, as the source
checksum in the SBOM. See D23.)

---

## D3 — One image per server

**Decision.** Separate images: `kea-dhcp4`, `kea-dhcp6`, `kea-dhcp-ddns`,
`kea-ctrl-agent` (3.0 only) and `kea-tools`.

**Why.**

1. **Drop-in replacement.** Someone using ISC's images should be able to
   change the registry name and nothing else. ISC ships one image per server,
   so we do too.
2. **Each server needs different permissions.** DHCPv4 needs `CAP_NET_RAW`;
   DDNS needs nothing. In a combined image, DDNS would get raw socket access
   for no reason.
3. **Each needs different networking.** The DHCP servers generally need
   macvlan, ipvlan or host networking to see broadcasts and multicast. DDNS is
   happy on a bridge.
4. **One process per container.** A combined image would need a process
   supervisor as PID 1, and your orchestrator would lose per-server restarts
   and health checks. ISC's Alpine images avoid this too; only their deb/rpm
   images use supervisord.

**What it costs: very little.** All the runtime images build on one shared
`runtime-base` stage. The registry stores those layers once, and pulling all
five downloads them once, so each extra image costs roughly one binary. Kea
is compiled **once** per architecture either way (`ninja install` produces
every server from one build), so there's no extra build time.

---

## D4 — One repository for all the images

**Decision.** One repository publishes every image.

**Why.** With one repository per image, the images couldn't share a build
stage, so Kea would be compiled five times per architecture: ten full
compiles per release. It would also mean five copies of `versions.json` and
five Renovate configs, and adding a Kea branch would touch five repositories
instead of one line.

GHCR packages belong to the account, not the repository, and link back
through the `org.opencontainers.image.source` label. So one repository can
publish `ghcr.io/dapalab/kea-dhcp4` and the rest with no trouble.

---

## D5 — A separate `kea-tools` image

**Decision.** Publish a fifth image with `perfdhcp`, `kea-admin`, `keactrl`
and `kea-lfc`, but **not `kea-shell`**.

**Why.** A load generator doesn't belong in a production DHCP server image,
but CI needs `perfdhcp` to test a real DHCP exchange. A separate image solves
that, and also fills a gap in ISC's images: their issue #46 is "database
schema update impossible because kea-admin missing in docker images".

**Why not `kea-shell`.** It's a standalone Python client for the control API,
and including it means including `python3`. Removing it took the image from
**122 MB to 65 MB**: more than half the image was there for one convenience
tool.

Nothing needs it. No binary or library refers to it, the HA hook
(`libdhcp_ha.so`) has no Python dependency and lives in the server images
anyway, and DDNS is server-to-server over UDP. All 33 control API commands work
through the `nc` every image already has for its healthcheck, so leaving it
out removes a wrapper, not a feature.

---

## D6 — Each branch lists its own images

**Decision.** Each branch in `versions.json` carries its own `images` list.

**Why.** `kea-ctrl-agent` exists in 3.0 (`src/bin/agent`) but was **removed in
3.2**, when the servers gained their own HTTP/TLS control sockets. We
confirmed this by unpacking both tarballs. So which servers exist depends on
the branch, and when 3.0 reaches end of life, retiring the agent is a
one-line change.

---

## D7 — Switch every optional feature on or off explicitly

**Decision.** Pass `-D mysql=... -D postgresql=... -D netconf=disabled
-D krb5=disabled` rather than relying on defaults. (MySQL and PostgreSQL were
later turned on: see D13.)

**Why.** Those four options are Meson `feature` options with no declared
default, which means `auto`. An `auto` feature switches itself on if its
dependency happens to be installed. Left alone, what ended up in the image
would depend on whichever `-dev` packages the build stage happened to have,
which is an easy way to get builds that quietly differ.

**A known difference from ISC, kept on purpose.** ISC's `kea-dhcp-ddns` image
includes `isc-kea-gss-tsig`. We disable `krb5`, so our DDNS image has no
GSS-TSIG hook.

We first flagged this as a gap to fix later, but on a closer look it isn't
one for most people:

- **GSS-TSIG** (RFC 3645) is Kerberos-based and used mainly with Active
  Directory DNS. It needs a KDC, a `krb5.conf` and a keytab.
- **TSIG** (RFC 2845) is the shared-key method used by BIND9, NSD, Knot and
  PowerDNS. It's **built into `kea-dhcp-ddns`**, not a hook, and it works in
  our image. We confirmed that by validating a full forward-and-reverse TSIG
  configuration against it.

If anyone needs GSS-TSIG, it would cost `kea-dhcp-ddns` only a little: 706 KiB
of `krb5-dev` at build time, 1.7 MB (amd64) or 2.3 MB (arm64) of `krb5-libs` at
runtime, and one Meson flag. Cheap, but no use to a BIND9 setup.

---

## D8 — Non-root, with file capabilities

**Decision.** Every image runs as user `kea` (UID/GID 10000). `kea-dhcp4` and
`perfdhcp` carry file capabilities, set with `setcap`.

**Why.** ISC's images run as root, so running as a normal user is a clear
improvement. There's one subtlety worth writing down: **in Docker,
`--cap-add` doesn't give a capability to a non-root process.** Added
capabilities go into the *container's* bounding and permitted sets, but a
non-root process only picks them up if the binary carries file capabilities,
or through ambient capabilities, which Docker doesn't offer.

So `setcap cap_net_raw,cap_net_bind_service=+ep` on the binary is what
actually makes non-root DHCPv4 work. Both capabilities are in Docker's default
set, so no `--cap-add` is needed. `--cap-drop=ALL` does remove them, though,
and the docs explain how to add them back.

**The minimum each image needs:**

| Image | Capabilities |
|---|---|
| `kea-dhcp4` | `CAP_NET_RAW`, `CAP_NET_BIND_SERVICE` |
| `kea-dhcp6` | `CAP_NET_BIND_SERVICE` |
| `kea-dhcp-ddns` | none |
| `kea-ctrl-agent` | none |
| `kea-tools` | `CAP_NET_RAW` (for `perfdhcp`) |

---

## D9 — A commented template as the default config

**Decision.** Each image's built-in config is a heavily commented template.
It defines no subnets and no DDNS domains, so the server starts, logs and
answers its control socket, but doesn't serve anything.

**Why a template rather than a bare minimal config.** An empty config would be
just as safe, but it wouldn't help anyone write a real one: it shows nothing
about pools, reservations or DDNS, which is most of what people need. The
template in the repository (`build/config/`) is the same file that's in the
image, so there's only one thing to maintain, and you can read it either on
GitHub or by `cat`-ing it out of an image you've pulled.

**Tested, not just written.** We uncommented the example subnet block, pointed
it at a test network and ran it: 9/9 REQUEST-ACK exchanges, with leases from
the pool. An example that's subtly wrong would do more harm than no example.

**Why this differs from ISC.** ISC's images ship a live `192.168.50.0/24`
pool on `eth0`. For an image people are encouraged to run on macvlan, that
means a new container could start answering DHCP on someone's network before
they've configured anything.

**No built-in credentials either.** ISC's 3.2 images include a password file
(`hiddens`) containing `api-user-name:api-user-password`, wired into the
default HTTP authentication. Credentials that anyone can read in a public
image aren't really credentials, so our default exposes only the Unix control
socket.

---

## D10 — Tags

**Decision.** Tags `3.2.0`, `3.2`, `3.0` and `3.0-lts`. No `latest`, and no
bare `3`.

**Why no bare `3`.** Today it would point at 3.2. But 3.0 is the long-term
support branch, and its end of life (June 2028) is **eleven months later**
than 3.2's (July 2027). Someone choosing `3` for stability would get the
shorter-lived branch, the opposite of what they wanted. `3` wouldn't say
anything `3.2` doesn't, and it could mislead.

**Why no `latest`.** A DHCP server quietly changing major version isn't
something most people want. ISC does publish `latest`, and it currently
points at 3.2.0 while 3.0 is the LTS, which is exactly the confusion above.

### Amendment, 2026-09-20: adding a tag that never moves

The reasoning above still holds. What it missed is that the scheme had **no
tag that stayed put.**

The weekly rebuild pushes every tag again with new bytes, by design, because
that's how Alpine security fixes reach people between Kea releases. So
`3.2.0`, which looks like a fixed version, changed every Monday, even though
the README said it didn't.

That had two real consequences:

1. Pinning `3.2.0` didn't give you a reproducible image.
2. A cosign signature belongs to specific bytes. After a rebuild, last week's
   signature is still valid, but `3.2.0` no longer points at those bytes. So
   the README's supply-chain story promised more than the rebuild schedule
   delivered.

**Decision.** Add a stamped tag that is never reused:

```
3.2.0-20260920-2244   never changes
3.2.0                 moves with each rebuild
3.2                   moves
3.0-lts               moves
```

ISC uses the same pattern on Cloudsmith (`3.1.9-20260527`): a fixed tag, with
the plain version moving on top. This extends the scheme rather than
replacing it: still no `latest` and no bare `3`.

**Why a time and not just a date, as ISC uses.** ISC releases rarely, so a
date is unique for them. Here it isn't: images rebuild weekly *and* whenever
a Renovate update merges, since an Alpine digest or GitHub Actions bump is a
push to `main` and so a build. **Four builds ran on 2026-09-20 alone** (as it
turned out, all from our own pushes: see D16's amendment). A
date-only stamp would have published `3.2.0-20260920` four times with
different contents, the opposite of what the tag is for. The first version of
this change did use date-only stamps; we corrected it the same evening.

Minute precision is enough. Runs on `main` are queued one at a time
(`concurrency: build-<ref>`, not cancelled outside pull requests), and a build
takes more than 17 minutes, so two runs can't tag in the same minute. That's
an argument rather than a guarantee, though, so `fuse` also asks the registry
and **fails if the stamped tag already exists**. Failing a publish is better
than overwriting a tag we've promised never to reuse. We checked it against
the live registry: it reports an existing tag as taken and an unused one as
free.

One thing to know: after "Re-run failed jobs", the run keeps its original
stamp, so this check will (correctly) refuse. "Re-run all jobs" runs the
`matrix` job again and gets a fresh stamp. The error message says so. (D27
explains why the stamp has to stay where it is for that advice to hold.)

**Considered: stop pushing the version tags again.** Then anyone on `3.2.0`
would never get an Alpine security fix, which defeats the weekly rebuild.
Moving tags are right here. What was missing was somewhere to stand still.

**Considered: just tell people to use digests.** Digests are the strongest
option and the docs still offer them. But a 71-character digest in a Compose
file is something people tend to avoid, and a pin nobody uses protects nobody.
A readable fixed tag is one people will actually use.

### Renovate can't follow stamped tags (documented)

A question from review: would someone's Renovate still offer updates if they
pinned a stamped tag? **No.** We checked by running the tag shapes through
Renovate's own `docker` versioning module:

| Pinned on | Renovate offers |
|---|---|
| `3.2.0` | `3.2.1` |
| `3.2` | `3.4` |
| `3.2.0-20260920` | nothing |
| `3.2.0-20260920-2244` | nothing |

`docker` versioning treats everything after the first `-` as a
*compatibility* label and only offers updates with the same label, so
`isCompatible("3.2.1-20261001-0417", "3.2.0-20260920-2244")` is `false`. It
doesn't raise an error, either. It just never offers anything.

That's true of any stamp, date-only included, so adding the time costs
nothing here.

**This doesn't argue against the tag.** Any fixed pin freezes you, and a
digest pin freezes you just as much. The real difference is narrower:
Renovate *can* update a digest pin and *can't* update a stamped tag. So they
suit different people, and the docs now say which is which:

- **If a bot manages your updates:** a moving tag plus `pinDigests`. Every
  deployment is reproducible, and the bot opens a PR for each rebuild and each
  version bump.
- **If you update by hand:** the stamped tag. It's readable and permanent,
  and the docs are clear that nothing will prompt you to move on from it.

If you do want to automate stamped tags, `regex` versioning works. We checked:
pinned on `3.2.0-20260920-2244`, it offers both `3.2.0-20261001-0417` and
`3.2.1-20261115-0417`. The docs include the snippet **with
`allowedVersions`**, because without that line the same config would offer
`3.0.4-…` → `3.2.0-…` and move someone off the LTS branch.

### Two date-only tags remain

The first build under this scheme published `3.2.0-20260920` and
`3.0.4-20260920` before we switched to minute precision. They're staying,
because **GHCR can't delete just a tag.** Removing one means deleting the
package *version*, and at the time that version was the same image `3.2.0`,
`3.2`, `3.0` and `3.0-lts` pointed at, so deleting it would have broken every
current tag (the same situation as D17).

They're harmless. Since the format changed, they can never be overwritten, so
they're permanently fixed tags from the scheme's first run.

**Where the stamp comes from.** The `matrix` job records one timestamp for
the whole run. Both the `created` label and the stamped tag come from it
(D21). Taking a fresh `date` in `fuse` instead could label an image one day
and tag it the next on a run that crosses midnight UTC. The same reasoning is
why amd64 and arm64 no longer stamp separately, minutes apart.

**Checked in `lint`.** `matrix.py check` asserts the tag list's shape
directly: exactly one stamped tag, listed first, no `latest`, no bare major
version, no duplicates. We confirmed it fires before trusting it. A published
tag can't be taken back, so this belongs in automated checks rather than
review.

---

## D11 — Native arm64 builds, no emulation

**Decision.** Build amd64 on `ubuntu-24.04` and arm64 on `ubuntu-24.04-arm`,
then combine them with `docker buildx imagetools create`.

**Why.** Kea is a large C++ codebase. On the development machine (a 4-core
Intel N97), a native amd64 compile takes about 77 minutes. Emulating aarch64
with QEMU is typically 5-10 times slower, which would push close to GitHub's
6-hour job limit for no benefit. GitHub's arm64 runners are free for public
repositories.

(As it turned out, the arm64 runners are the *faster* of the two, at about 18
minutes against about 27 for amd64.)

---

## D12 — Licence: MPL-2.0

**Decision.** License this repository under MPL-2.0. Don't copy anything from
`kea-docker`.

**Why.** `kea-docker` has no LICENSE file on any branch, only
`SPDX-License-Identifier: MPL-2.0` headers on its three Dockerfiles. MPL-2.0
works file by file: copy a file and that file stays MPL-2.0 with its notice,
but the rest of the repository is unaffected.

We don't copy anything. There's no entrypoint script to borrow (ISC's Alpine
images use a plain `CMD`), and what we share with them (`FROM`, `VOLUME`,
`EXPOSE`, `CMD`) is information about how Kea runs rather than creative
work. We follow the same *conventions* (paths, ports, volume layout) and write
our own files. They carry MPL-2.0 headers anyway, which costs nothing and
settles any question.

**Separately:** Kea itself is MPL-2.0 and we redistribute its binaries, so its
`COPYING` and `AUTHORS` files ship in every image at
`/usr/share/licenses/kea/`.

---

## D13 — MySQL and PostgreSQL support

**Decision.** The MySQL and PostgreSQL backends are compiled in. `kea-tools`
includes `kea-admin`, the `mysql` and `psql` clients it relies on, and the
schema SQL.

**This changed from the original plan**, which said "no MySQL/PostgreSQL in
v1 *unless it's nearly free to add*". It turned out to be nearly free (see
below), so we should have added it the first time we measured.

**What it costs, from Alpine 3.24's package index:**

| | Build stage | Runtime |
|---|---|---|
| MySQL | `mariadb-connector-c-dev` 244 KiB | `mariadb-connector-c` 691 KiB |
| PostgreSQL | `libpq-dev` 1.87 MiB | `libpq` 386 KiB |

About **1 MB extra at runtime**, plus two Meson flags. Kea finds both through
pkg-config (`dependency('mariadb')`, `dependency('libpq')`), so no special
build setup is needed.

The servers link `libmariadb` and `libpq` directly and never call a
command-line client. So the server images need no client tools at all, and a
build report showing `MySQL: yes`, plus a `type: mysql` config passing
`kea-dhcp4 -t`, shows the backend is compiled in and registered.

### Why `kea-tools` includes the clients

`kea-admin` calls out to `mysql` (in 30 places) and `psql` (in 5). Without
them it would look usable but couldn't do its main job.

Installing the full client packages costs **65.6 MB**, because
`mariadb-client` is seven similar ~5 MB tools (`mariadb-dump`,
`mariadb-check`, `mariadb-import` and so on) that `kea-admin` never uses.
Copying just the two binaries it does use costs **8.8 MB**, measured against
an image matching `runtime-base`:

| | Image | Extra |
|---|---|---|
| runtime-base equivalent | 20.8 MB | — |
| **+ `mariadb` and `psql` binaries** | 29.6 MB | **+8.8 MB** |
| + full client packages | 86.4 MB | +65.6 MB |

Their other dependencies (`libcrypto`, `libssl`, `libstdc++`, `libz`,
`libgcc`) are already in `runtime-base`. `mysql` is a symlink to `mariadb`,
and the deprecation notice it prints goes to **stderr only**, so `kea-admin`'s
parsing of its output isn't affected.

The flip side is that the backup tools aren't there, so a backup has to be
taken with the database's own image. [Upgrading](upgrading.md) explains how.

### Size turned out not to matter much

`kea-tools` isn't something you leave running. It's a `docker run --rm` for
one-off jobs, and it shares `runtime-base` with the server images, so its
real cost is the difference over layers you already have. We kept the
two-binary approach because it's free, not because the size was a problem.

---

## Phase 1 results

Built and tested on the development machine (Intel N97, 4 cores, 5.7 GB RAM,
WSL2) for `linux/amd64`, Kea 3.2.0 on Alpine 3.24.

### Build

| | |
|---|---|
| Kea compile (652 objects, `-j3`) | **51 min** |
| Each runtime image after that | **2-10 s** |

The compile is cached as one builder stage, so building all four images
costs one compile (every later target showed `#13 CACHED`). This is the
measurement behind D3 and D4: one image per repository would have meant four
compiles instead of one.

### Image sizes

| Image | Reported | Notes |
|---|---|---|
| `kea-dhcp-ddns` | 58 MB | |
| `kea-dhcp6` | 68 MB | |
| `kea-dhcp4` | 68 MB | |
| `kea-tools` | 65 MB | was 122 MB before `kea-shell` and `python3` were removed |

The download size tells the more useful story:

| | |
|---|---|
| `kea-dhcp4` alone | 18.9 MB |
| All four together | **27.4 MB** |

All four share four layers (the Alpine base, runtime packages, Kea's
libraries and the licence files), so pulling all four costs about 1.5 times
one image, not 4 times.

### Two bugs the tests caught

Both would otherwise have turned up in production.

**1. Kea 3.x refuses a control-socket directory more open than 0750.**
`mkdir -p` creates 0755, and Kea stops at startup with
`socket path:/run/kea does not exist or has more relaxed permissions than 750`.
It's the same security change behind ISC's kea-docker#45. Fixed with an
explicit `chmod 0750` on `/run/kea` and `/var/lib/kea`.

**2. `set -o pipefail` with `grep -q` can fail at random.** `grep -q` stops at
the first match, `docker logs` then gets SIGPIPE, and pipefail reports the
whole pipeline as failed even though the match succeeded. The readiness check
was passing by luck and would have been flaky in CI. Logs are now captured to
a variable and matched with `case`.

### What we confirmed

| Claim | Result |
|---|---|
| Servers run as non-root | `uid=10000(kea)` |
| File capabilities are present | `cap_net_bind_service,cap_net_raw=ep` |
| Healthcheck reports healthy | all three server images |
| `--cap-drop=ALL` stops dhcp4 | `exec: operation not permitted` |
| Adding both capabilities back works | starts normally as UID 10000 |
| `CAP_NET_RAW` alone isn't enough | it fails, so both really are needed |
| A DHCPv4 exchange completes | 49/49 REQUEST-ACK, about 0.7 ms each |

It's reassuring how the capability problem fails: a binary whose file
capabilities aren't available fails straight away at `exec`, with
`operation not permitted`. That's much easier to diagnose than a server that
starts but can't bind its port.

### A known limitation

The DHCP exchange test uses `dhcp-socket-type: udp`, so that `perfdhcp` can
reach the server on a bridge network. That means it **doesn't** test the raw
socket path a real macvlan setup uses. Testing that needs a real L2 network
with broadcast, which a standard CI runner can't provide. The `CAP_NET_RAW`
grant is checked separately (above), and the raw path was later confirmed by
hand on a Raspberry Pi 5.

---

## D14 — `install_umask=0022`

**Decision.** Pass `-D install_umask=0022` to `meson setup`.

**Why.** Kea **3.0.x** sets `'install_umask=0027'` in its `project()`
`default_options`, which installs every library as `0750 root:root`. Our
servers run as UID 10000, so they couldn't read their own libraries and failed
at startup with:

```
Error loading shared library libkea-util.so.103: Permission denied
Error relocating /usr/sbin/kea-dhcp4: ... symbol not found
```

The relocation errors are a side effect: the loader couldn't open the
libraries in the first place.

**Kea 3.2 removed that setting**, using explicit
`install_mode: 'rwxr-x---'` on its state directories instead. So 3.2
installed libraries as 0755 and worked, while 3.0 didn't, which is why CI
failed on exactly one branch, on both architectures.

**ISC's images don't hit this** because they run as root. It's a direct result
of D8, and a good reminder that the two stable branches can't be assumed to
build the same way.

**Checked with a minimal Meson project:**

| | Installed mode |
|---|---|
| `install_umask=0027` | `750` |
| `install_umask=0022` | `755` |
| `project()` says `0027`, command line says `0022` | **`755`**: the command line wins |

The last row is the important one: Kea sets the value as a default, and `-D`
on the command line overrides it.

We set it for both branches on purpose, so file permissions are something our
build decides, not something inherited from whichever branch we're
compiling.

---

## D15 — OCI image labels

**Decision.** Every image carries the standard `org.opencontainers.image.*`
labels, with `source` pointing at this repository.

**Why it matters.** `org.opencontainers.image.source` is what links a GHCR
package to its repository. Without it, the package doesn't inherit the
repository's permissions, and our first publish failed with:

```
denied: permission_denied: write_package
```

even though the job had `Packages: write` and `docker login` had succeeded.
We hadn't set any labels at all.

**The "unofficial" note travels with the image.** Both `vendor` and
`description` say these are community builds not affiliated with ISC, so
`docker inspect` shows it even to someone who never saw the README. An image
can end up a long way from where it was found, so that's a good place for it.

**Build details** (`version`, `revision`, `created`) come from the workflow:
`matrix.kea`, `github.sha` and the build timestamp. So every image records
exactly which commit and which Kea release produced it.

---

## D16 — Renovate, self-hosted as a GitHub App

**Decision.** Renovate runs from `.github/workflows/renovate.yml` with a
GitHub App token, rather than `GITHUB_TOKEN` or the hosted Renovate app.

**Why self-hosted.** No third-party service needs access to the repository,
and its schedule sits next to the other scheduled jobs (weekly rebuild,
upstream watcher), where they can all be seen together.

**Why an App and not `GITHUB_TOKEN`.** This is a GitHub limitation rather than
a preference: **pull requests opened with `GITHUB_TOKEN` don't trigger other
workflows** (GitHub does this to prevent loops). A Renovate PR bumping Kea
would never have `build.yml` run against it, so the update would arrive
untested. An App token isn't `GITHUB_TOKEN`, so the limitation doesn't apply.

**Why an App and not a personal access token.** A token would also trigger
workflows, but the App is better in three ways. Its tokens are created per
run and expire within the hour, so only a private key is stored. Its PRs come
from a bot identity, not a person's account. And it's limited to this one
repository. To be fair, the private key is still a stored secret: better
scoped and separately revocable, but not gone.

**The App needs `Workflows: write`**, which is easy to miss. Renovate edits
`.github/workflows/*.yml` to update pinned actions, and without it those PRs
fail with a confusing push error.

### What gets merged automatically, and why

| | Automerge? | Reasoning |
|---|---|---|
| Kea version | **never** | Updating a DHCP server is a human decision, however green CI is. Waits 3 days after release. |
| Alpine **digest** | yes | This is how Alpine security fixes reach the images between releases. |
| Alpine **version** (3.24 → 3.25) | no | A new Alpine release can change compiler and library versions under the build. Waits 7 days. |
| GitHub Actions | yes | Grouped, pinned to commit hashes, waits 3 days. |
| **Runner images** (`ubuntu-24.04`) | **no** | Renovate also tracks `runs-on:`. A dry run showed it grouping these with action updates, where they'd have been merged automatically, quietly changing the OS the build runs on. Waits 30 days. |

### Keeping each branch on its own track

Both entries in `versions.json` point at the same upstream repository, so they
would clash under one dependency name. The custom manager gives them distinct
names (`kea-3.0` and `kea-3.2`), while `packageNameTemplate` holds the real
lookup.

`allowedVersions` then keeps each to its own minor version: `>=3.0.0 <3.1.0`
and `>=3.2.0 <3.3.0`. That stops the LTS entry being offered 3.2.x, and it
means **odd-numbered development releases are never proposed**, since 3.1 and
3.3 fall outside both ranges. A future 3.4 is excluded too, which is what we
want: moving to a new stable branch is a human decision, and the upstream
watcher opens an issue when one appears.

We checked this against the real files: the manager finds exactly two Kea
dependencies with the right names and values, two `alpineRef` matches and one
Dockerfile `ARG` match.

### Amendment, 2026-09-21: Renovate had never opened a PR

A forced update from the dashboard created a branch but no PR, which led us to
look properly. Renovate had **never** opened a PR or landed a commit here,
while every one of its workflow runs reported success. So no Alpine digest or
action update had ever arrived through it, and none of the builds on
2026-09-20 came from it; they were all our own pushes. (The Monday rebuild
was still picking up Alpine package fixes, so the images hadn't stood still.)

A debug run showed why. Every update rule here has a minimum release age, so
Renovate sets a `renovate/stability-days` status on each branch. The App had
no permission to set commit statuses:

```
POST .../statuses/<sha> = statusCode=403
GitHub failure: Resource not accessible by integration
Caught error setting branch status - aborting
Repository result: repository-changed
```

Renovate then abandons the **whole run**, not just that branch, and exits
successfully, so nothing looked wrong. The fix is one more permission for
the App: **Commit statuses: Read and write**, alongside Contents, Pull
requests, Issues and Workflows. It's the same pattern this log keeps finding:
a job that had never been seen to do its job, reporting success.

**Fixed and confirmed the same day.** With the permission added, the next run
logged `PR created` and finished normally. PR #5 (`renovatebot/github-action`
v46.3.3) was opened by the Renovate App, passed `required` and merged itself:
the first dependency update ever to arrive this way.

It merged while `renovate/stability-days` was still pending, because it was
forced from the dashboard, which skips the release-age wait on purpose. Branch
protection only requires `required`, so we checked that a normal update can't
merge early: updates that haven't reached their age are held as
`pendingChecks`, and Renovate doesn't create their branch until either the
age passes or someone ticks the box. The un-ticked runner-image update was
the control: same state, no branch, no PR.

---

## D17 — Cleaning up old package versions safely

**Decision (2026-09-20).** No automatic cleanup for now. Written down because
the obvious way to do it would break every published image.

**Why "delete untagged versions" is dangerous here.** A multi-arch tag points
at an OCI **index**. Only the index carries the tag. The per-architecture
images and the SBOM/provenance attestations underneath it have no tags, by
design. From the live packages:

```
kea-dhcp4:3.2  ->  sha256:2396fd43...  amd64/linux    tagcount=0
                   sha256:90919455...  arm64/linux    tagcount=0
                   sha256:ef5e6d2b...  attestation    tagcount=0
                   sha256:b1e74fe2...  attestation    tagcount=0
```

So the usual "delete all untagged versions" cleanup, which is what most GHCR
cleanup actions offer, would delete the amd64 and arm64 images out from under
`kea-dhcp4:3.2`. The tag would still exist, but it would point at an index
whose contents were gone, and every pull would fail in a way that looks like
registry corruption.

**How cleanup has to work:** decide what to keep by what's *reachable*, not by
what's tagged.

1. List every tag on the package.
2. Follow each one to its index and collect everything it refers to.
3. Keep the tagged versions **and everything they refer to**.
4. Delete only what's outside that set, **and older than a grace period**.

**The grace period, as planned.** The docs recommend pinning digests, so
deleting an old digest would break exactly the people who followed that
advice. The plan was 90 days, so a pin would survive at least a quarter. See
the amendments below for what actually happened.

**Why wait.** Public GHCR storage is free, so the cost of leaving things is
clutter, not money. Versions build up at about 7 per package per weekly
rebuild, roughly 360 a year each, which is fine for a year or two.

### Amendment, 2026-09-20: measured, and a collector written

Measured before any cleanup:

| Package | Versions | "Delete untagged" would remove | ...of which were in use |
|---|---|---|---|
| `kea-dhcp4` | 136 | 94 | **48** |
| `kea-ctrl-agent` | 68 | 47 | **24** |

**About half of the untagged versions were in use**: a per-architecture image
or an attestation under a working tag. The default setting of practically
every off-the-shelf cleanup action would have broken all five images at once.

`scripts/gc-packages.py` works out reachability instead. It keeps anything
tagged, anything an index points at, and any signature whose image survives.
Everything else is left over from a build whose tags have moved on.

Signatures need care: cosign stores each one under a tag named after the
image it signs, so deleting an old image without its signature would leave a
signature pointing at nothing. The collector deletes them together. (This
amendment originally said those tags end in `.sig`. They don't: cosign's
current format uses no suffix, and the collector couldn't see them until D26
fixed it.)

It has three safety refusals, each tested by breaking the thing it guards:

| Refuses when | Tested by |
|---|---|
| more than 50% of a package would go | switching off the reachability walk: it stopped at 47/68 rather than deleting the package |
| the registry can't be read | a wrong owner name: `403`, exit 1, nothing deleted |
| a build is running | running it during a build: `fuse` pushes an index and *then* tags it, so a walk in between could mistake a live index for garbage |

We also checked its plan with a separate reachability walk that doesn't reuse
the collector's own result: of the 207 versions it proposed deleting across
the five packages, **none** were reachable from any tag.

It's a dry run by default; deleting needs `--delete`. It runs by hand, and
should stay that way until it's been used enough times to be boring.

### Amendment, 2026-09-21: what the first run actually did

The collector was fixed (D26) and run for real on 2026-09-21. **It does not
implement the grace period described above**, and that wasn't noticed until
afterwards. So the first run deleted everything unreachable regardless of
age, including three builds from 2026-09-20 that had been published for less
than a day. Anyone who had pinned one of those builds' digests in that window
would find them gone.

In practice, stamped tags (D10) now keep every published build reachable
forever, so the only versions the collector ever finds are ones nobody could
reasonably have pinned: the per-architecture wrapper indexes each build
leaves behind (D26). But the gap between what was written here and what the
code does is recorded honestly rather than quietly removed.

**Then fixed.** The collector now has `--min-age` (default 90 days). A
version younger than that is kept even if nothing points at it, and it also
counts as a starting point for the walk, so everything it refers to and its
signatures are kept too. That second part matters: a young index can point at
*older* manifests (identical bytes are reused between builds), and exempting
only the young version would have deleted its contents.

Five tests cover it, including one at 89 days and one at 91, which pins the
default at exactly 90. Each piece was checked by breaking it: removing the
grace period, exempting young versions without walking them, inverting the
cutoff, ignoring `--min-age`, walking signatures before young versions, and
changing the default to 30 or 120 days were each caught by a named test.
`--min-age 0` turns it off, for a deliberate clean slate.

---

## D18 — Keyless signing

**Decision.** Every published image is signed with cosign using the build
workflow's own identity. No signing key is stored anywhere.

**Why keyless.** A signing key kept as a repository secret has to be
generated, stored, rotated and protected, and if it leaked, nobody might
notice. Keyless signing ties each signature to the workflow that made it
(`.../build.yml@refs/heads/main`) and records it in Sigstore's public
transparency log. There's nothing to leak.

**Sign the image, not the tags.** One signature on an index covers every tag
pointing at it, so `3.2` and `3.2.0` share one. It also stays valid when a
moving tag later points somewhere else, because it belongs to the bytes, not
the name. (D25 later added the two platform images inside each index.)

**Signing comes last**, after the multi-arch check. A signature says "this
CI produced these bytes", so it shouldn't go on an image we haven't yet
confirmed was put together correctly. The workflow then verifies its own
signatures before finishing. (D25 found that check couldn't actually fail,
and fixed it.)

**The chain of trust.** ISC signs the source → the build checks that
signature against the stored keys and stops if it fails → CI signs the
resulting image → you verify that signature against this repository. Each
link can be checked without trusting any of the others.

**What it doesn't claim.** Only where the image came from and that it hasn't
changed. It says nothing about ISC endorsing these images. For an unofficial
image, "who actually built this?" is the most useful question to be able to
answer, and that's what it answers.

---

## D19 — No automated DHCPv6 exchange test

**Decision.** `kea-dhcp6` is covered by gate 5 (it starts and its control
socket answers), but there's no automated DHCPv6 exchange test. That's a
deliberate choice.

**We tried, and stopped.** DHCPv6 is built around multicast: a client sends
SOLICIT to `ff02::1:2` from a link-local address on a real L2 network.
Docker's bridge IPv6 doesn't reproduce that. A prototype ran into two
problems:

1. **Kea couldn't bind its link-local multicast socket** at startup
   (`Failed to bind socket to fe80::.../port=547: Address not available`).
   That's a duplicate-address-detection race: the container's link-local
   address is still "tentative" when Kea starts. A 5-second delay made it go
   away, which confirmed the cause.
2. **Even with the sockets open, perfdhcp got no answers**: 20 SOLICITs sent,
   0 ADVERTISEs back, because the workarounds needed to avoid multicast don't
   match how Kea expects to be addressed.

Going further would mean a test with a `sleep` for address detection and more
and more artificial addressing. At that point it would be testing our
workarounds, not the image. A flaky test is worse than a clearly documented
gap.

**It's the same limit as the DHCPv4 raw-socket path**, which is tested over
UDP because a Docker bridge has no broadcast. Accepting the equivalent limit
for v6 is consistent, not a lower standard.

**What is covered.** Gate 5 proves the server starts with its built-in config
and answers its control socket. The v4 exchange test exercises the shared
`libkea` code (the allocation engine, lease manager and config parser), which
v6 uses too. What's untested is the v6 packet handling specifically.

**Where it should be tested: real hardware**, on a real network, the same way
the v4 raw-socket path was confirmed on a Raspberry Pi. It's written down here
so anyone relying on the v6 image knows exactly what CI does and doesn't
cover.

---

## D20 — Branch protection

**Decision.** `main` requires a check named `required` to pass. Native
auto-merge and branch deletion are on, and Renovate uses the platform's
auto-merge.

**Why.** Renovate merges Alpine digest and GitHub Actions updates by itself.
Without a required check, the only thing stopping a bad merge was Renovate
checking CI status itself, and if a workflow failed to start, it would see no
failures and merge. For a repository publishing DHCP server images
unattended, that protection should come from GitHub itself.

### Why one stable check name

The build jobs' check names include the versions:

```
build (3.2, 3.2.0, alpine:3.24@sha256:294b68...)
```

Those change whenever `versions.json` does, which is exactly what Renovate
changes. Requiring them directly would leave branch protection waiting forever
for a check under its old name, so **the first Kea update would have blocked
itself.**

So the `required` job depends on `lint`, `matrix` and `build`, always runs
(`if: always()`), and fails if any of them failed, was cancelled or was
skipped. Its name never changes.

### The settings

| Setting | Value | Reasoning |
|---|---|---|
| `checks` | `required` only | a stable name; see above |
| `strict` | **false** | `true` makes every PR rebase whenever `main` moves. With Renovate opening several PRs at once, that's constant churn for no safety benefit here. |
| `enforce_admins` | **false** | Keeps an emergency route for the owner. It doesn't weaken the Renovate protection: Renovate acts through a GitHub App, not as an admin, so it's bound by the check either way. |
| `required_pull_request_reviews` | none | A solo project. Requiring a review you'd give yourself adds nothing, and it would block automerge entirely. |
| `allow_force_pushes` | false | Protects history, and the commit signatures on it. |
| `allow_deletions` | false | `main` can't be deleted. |

**The upshot:** Renovate can't merge anything unless `required` passes. The
owner can still push directly, on purpose: the automation is constrained, and
the human isn't.

---

## D21 — Prove the published image is the tested one

**Decision.** Compute the build timestamp once and use it for both the test
build and the push build. After pushing, check that the published image is
byte-identical to the one the tests ran against.

**The problem.** `CREATED` was computed separately for the test build and the
push build, minutes apart, with the smoke tests in between. It feeds the
`org.opencontainers.image.created` label, so the two builds produced slightly
different images. "Tests run before anything is pushed" was true, but the
stronger thing people read into it, *what we shipped is what we tested*,
wasn't.

In practice the only difference was one timestamp. The case that really
matters is rarer: if the build cache were missed during a long test run, the
push build would recompile Kea from scratch and publish a genuinely different
build, and nothing would notice.

**Why the obvious check doesn't work.** Comparing the two
`containerimage.digest` values from buildx's metadata would fail *every
time*, because they describe different things:

| Build | What `containerimage.digest` reports |
|---|---|
| `--load` (test) | the **image manifest** digest |
| registry push | the **index** digest |

A registry push wraps the image in an index alongside the provenance and SBOM
attestations (which have platform `unknown/unknown`). We checked this against
a local registry: the index digest changes depending on whether attestations
are on, while the image inside it is identical either way and matches the
`--load` digest.

So the review's suggestion, "attestations are separate, so they don't change
the digest", was right about the image and wrong about the *reported* digest.
The check compares like with like: the image inside the index, picked out by
`platform.os != "unknown"`.

**Tested by simulation** against a local registry:

| Case | Result |
|---|---|
| same `CREATED` (the fix) | digests match, publishing continues |
| different `CREATED` (the old bug) | the check fails |
| cache miss, different content | the check fails |

So we saw it fail for the right reason, twice, before trusting it.

**The trade-off we accept.** A false alarm blocks a publish rather than
shipping untested bytes, which is the right way round. If a genuine cache miss
ever trips it, the answer is to re-run the job, not to loosen the check.

---

## D22 — Test that `kea-lfc` really compacts the lease file

**Decision.** A sixth smoke test runs `kea-dhcp4` with `lfc-interval: 5`,
feeds it leases, and checks that the lease file was actually compacted. It
includes a negative control that swaps `kea-lfc` for a stub that fails.

**The gap.** `kea-lfc` ships in `dhcp4`, `dhcp6` and `tools`, and both config
templates run it hourly, but nothing ever tested it. At `lfc-interval: 3600`
it wouldn't even run during a CI job.

**The review expected the wrong failure, and that changed the test.** It said
a missing `kea-lfc` would "fail silently". We measured: it fails *loudly*, at
startup, before serving anything:

```
DHCPSRV_MEMFILE_FAILED_TO_OPEN Could not open lease file:
    File not found: /usr/sbin/kea-lfc
DHCP4_INIT_FAIL failed to initialize Kea server
```

Gates 4 and 5 already catch that, because the server never reaches
`DHCP4_STARTED`. A test for a missing binary couldn't have failed on its own.

**What does fail silently is a `kea-lfc` that's present but broken**: a
missing library, the wrong architecture, a directory it can't write to. Kea
runs it and **never checks whether it succeeded**. Measured with a stub that
exits 1:

| | Working `kea-lfc` | Broken `kea-lfc` |
|---|---|---|
| container | healthy | **healthy** |
| logs | `LFC_START` / `LFC_EXECUTE` | **the same, no error** |
| `kea-leases4.csv.2` (compacted) | present, one row per address | **missing** |
| `kea-leases4.csv.1` left behind | no | **yes** |
| stale `.pid` | no | **yes** |

Nothing shows until the lease file has grown for weeks and restarts slow
down.

**So the test checks the compacted file**, not the binary: `.2` has to exist,
contain the leases just added, and hold exactly one row per address. It can
only get into that state if `kea-lfc` ran to completion. A file with duplicate
rows means a rotation that was never processed.

**The negative control runs every time.** It builds a variant image with
`kea-lfc` replaced by `exit 1`, confirms the test goes red, and confirms the
failure really is silent in the logs. It runs on every CI build, on both
architectures, so the test can't quietly stop telling the difference.

**Cost.** The smoke suite goes from about 40 s to about 81 s. It waits by
polling, not with a fixed sleep, so a slow runner won't make it flaky.

**Considered: checking the `LFC_*` log lines.** Easier, but weaker.
`DHCPSRV_MEMFILE_LFC_START` is logged by `kea-dhcp4` *before* it runs
`kea-lfc`, so it appears even when the binary is broken. It shows Kea tried,
which isn't the question.

**Considered: a second config file for the test.** The variant is generated
from `test/kea-dhcp4-smoke.conf` with `sed` instead, so the two can't drift
apart.

---

## D23 — Put Kea in its own SBOM

**Decision.** The build generates an SPDX document describing Kea and ships
it at `/usr/share/sbom/kea.spdx.json`. BuildKit's scanner merges it into the
published SBOM.

**The problem.** `buildx --sbom=true` lists what the *package manager*
installed. Kea is compiled from source and copied in from a build stage, so it
didn't appear at all. On the published `kea-dhcp4:3.2`:

- 25 apk packages were listed (`busybox`, `tzdata`, `ssl_client` and so on)
- the word `kea` **didn't appear anywhere in the SBOM**
- even though the image contains 23 `libkea-*.so` libraries, the server
  itself and a dozen hook libraries

An SBOM that lists `tzdata` but not the DHCP server gives a false sense of
completeness. The review flagged this to check first; it was real.

**One correction to the review.** It suggested adding Kea *plus Boost and
log4cplus*. `ldd` on the server shows **no Boost at all**: Kea only uses
Boost's headers, so there's nothing to list at runtime. `log4cplus` and
OpenSSL are apk packages and were already listed correctly. Kea was the only
thing missing.

**How it shows up.** Checked against a real build: with the document in
place, the published SBOM gains a `kea` package with its version, MPL-2.0,
the upstream URL and a CPE. The CPE matters beyond record-keeping:
`cpe:2.3:a:isc:kea:3.2.0` is what lets a vulnerability scanner match Kea's
CVEs at all, which D24 depends on.

One detail: the merge drops SPDX's `checksums` field, so the source hash
survives in the package URL (`pkg:generic/kea@3.2.0?checksum=sha256:…`)
instead. The document inside the image keeps both.

**This is where the hash from D2 belongs.** That amendment removed a
`sha256sum` that compared the hash with nothing. Recording the same hash here,
as the checksum of the source the binaries were built from, is a genuine use
for it: a record of where the image came from, rather than a check that
isn't one.

**Reproducible output.** The document has no live timestamp. It uses
`SOURCE_DATE_EPOCH`, defaulting to the epoch, because a timestamp would make
the test and push builds differ and trip D21's check.

**Checked in two places, because they're two different claims:**

| Where | What it proves |
|---|---|
| smoke gates 2 and 5 | every image *ships* a valid document matching its version |
| `fuse`, after tagging | the *published SBOM* lists Kea at the right version |

The first proves the input is there; only the second proves buildx merged it.
A scanner silently skips a document it can't read, so without the second
check, Kea could quietly drop out of the SBOM with a green build.

**Two of our own mistakes, both the familiar pattern.** The validator started
as an inline `python3 -c` with a quoting error, and the calling code threw
away stderr, so it failed for a reason nobody could see. It's now
`test/check-sbom-doc.py`, a real file with its own tests. The first version of
*those* tests passed a command string that never ran, so all three cases
reported "not valid JSON": three green ticks, none testing what it claimed.
The tests now check for the *expected message*, not just a failure.

---

## D24 — Daily vulnerability scans, with grype

**Decision.** A daily workflow scans the **published** images and opens an
issue for anything High or above, or if the scan itself couldn't run.

**Why scan what's published, not the build.** A build-time scan answers "was
it clean when we built it?". The more useful question is "is what people are
pulling right now affected?", and the answers drift apart as soon as a new
Alpine CVE appears after a build. The weekly rebuild fixes that; this tells
us how far behind things get in the meantime.

**Why grype and not trivy: we measured.** Against an SBOM naming Kea 1.4.0,
which has known CVEs:

| Tool | Result |
|---|---|
| `trivy sbom` | 0 findings; `--list-all-pkgs` shows it reads **no packages at all** from the document |
| `grype sbom` | CVE-2018-5739, CVE-2019-6472, CVE-2019-6474 |

grype can match CPEs for software it can't place in a package ecosystem,
which is exactly the case for anything built from a tarball. The CPE from D23
is what makes that possible; without it, neither tool would see Kea.

**Two scans per image, because one isn't enough.** Tested with an image
containing a deliberately old Kea:

| Scan | Finds |
|---|---|
| `grype <image>` | apk packages |
| `grype sbom:<the document the image ships>` | Kea itself |

Scanning the image **doesn't** read the SPDX document inside it: the image
scan found 0 Kea CVEs, and the document scan found 3. Since Kea is built from
source, skipping the second scan would miss the component most worth
watching.

The document is checked with `test/check-sbom-doc.py` before scanning,
because a document grype can't read produces zero findings, which looks just
like good news.

**A partial scan is reported as broken, even if it found things.** If any
image can't be scanned, the run says so up front, even when other images had
findings. Otherwise one image failing to scan while another has a CVE would
be filed as "vulnerabilities found", and the gap in coverage would go
unmentioned *because* something else was reported.

**It found something straight away.** The first run against `kea-dhcp4:3.2`
reported `CVE-2026-85091` in `zlib 1.3.2-r0`, High, with **no upstream fix
available** yet. trivy reported nothing for the same image. That difference
is why the scanner is pinned by digest (Renovate keeps it up to date): "no
findings" can mean different things from one tool or version to the next, and
a silent change of tool would silently change what a green run means.

**An ordering detail.** The scan needs images that include the D23 SBOM
document. Until a build had republished them, it correctly reported itself as
broken, because the document really was missing. We confirmed that by running
it against the images published before D23.

---

## D25 — What gets signed

**Decision.** Sign each multi-arch index and the two platform images inside
it, by digest, rather than using `cosign sign --recursive`.

**What `--recursive` was costing on GHCR.** Each index holds four manifests:
`linux/amd64`, `linux/arm64`, and one SBOM/provenance attestation per
architecture. `--recursive` signed all five. And on GHCR, each signature
takes **two** package versions, not one. cosign stores signatures in the
Sigstore bundle format as an OCI 1.1 "referrer", but GHCR doesn't support the
Referrers API (`/v2/<name>/referrers/<digest>` returns 404), so cosign falls
back to a tag-based scheme:

| Version | Tagged? | Contents |
|---|---|---|
| bundle | no | `artifactType: application/vnd.dev.sigstore.bundle.v0.3+json`, `subject: <digest>` |
| referrers index | `sha256-<hex>` | lists the bundle |

That's 10 package versions per index, 20 per package per build, and about 90
per build across all five packages. Now it's 6, 12 and 54.

**Why not just the index (2 per index)?** That covers the usual way of
checking, since a tag points at the index. But someone who pinned a single
architecture's digest (which is what a node actually pulls) would get "no
signatures found" for a genuine image. Verification that only works if you
pinned the right kind of digest would be confusing, and four small extra
manifests per index is a fair price to avoid that.

**Why not the attestations?** They're build metadata that nobody pulls, and
they're already tied to their image by the signed index that lists them.

**Note the tag format.** These signature tags are `sha256-<hex>` with **no**
`.sig` suffix; the older cosign format used `sha256-<hex>.sig`. Anything that
recognises signatures by their suffix, including `scripts/gc-packages.py` at
the time, won't see these. (D26 fixed the collector.)

Confirmed on the live registry after the change: a build added 26 versions to
`kea-dhcp4` instead of 34, an arm64 digest verified from outside CI, and an
attestation manifest correctly reported no signature.

### The signature self-check couldn't fail

D18 says the workflow verifies its own signatures before finishing. The loop
did this:

```bash
cosign verify "$ref" ... > /dev/null && echo "  ok  $ref"
```

A command on the left of `&&` is exempt from `set -e`, so every signature but
the last could fail to verify and the step would still pass. We showed it by
running the step, taken straight from `build.yml`, with a stub `cosign` that
fails for the first of two images: no "ok" line for it, and exit 0.

The rewritten step reports `::error::signature does not verify: <subject>`
and exits 1 in the same test. It also refuses an index that doesn't contain
exactly two platform images, so it can't sign too much or too little.

---

## D26 — Making the package collector trustworthy

**Decision.** `scripts/gc-packages.py` reads every page of the registry's tag
list, refuses to run unless the registry and GitHub agree on the set of tags,
recognises the signature tags cosign actually writes, and won't delete more
than 25% of a package unless told to for a single run.
`test/gc-packages-test.py` tests all of that in `lint`, with no network
access.

**The tag list was a single request.** The registry splits `tags/list` into
pages, and a tag the collector never sees can't protect its image. So a short
list doesn't make the collector do less. It makes it **delete images that are
still in use**, in amounts the old 50% limit wouldn't catch. Checked against
GHCR on 2026-09-20 (`kea-dhcp4`):

| Check | Result |
|---|---|
| `Link` header on a normal request | none |
| tag count, registry vs `gh api` | 71 vs 71 |
| `tags/list?n=5` | 5 tags, plus `Link: </v2/dapalab/kea-dhcp4/tags/list?last=3.0-lts&n=5>; rel="next"` |

So the registry does paginate. The default page just happened to hold
everything, and the tag count grows with every build, so this would have
started biting around the time cleanup became worth doing. Now:

- every `rel="next"` link is followed (it's a *path*, resolved against the
  registry)
- a `Link` header it can't parse, a page that repeats a tag, or more than
  1000 pages stops the run rather than returning a partial list
- the registry's set of tags must **match** the set GitHub's package API
  reports. Sets, not counts, since a count can match while the tags differ.
  When two sources disagree about what's in use, nothing should be deleted,
  and this is the only check that catches a list that's short *without
  saying so*.

**It had never recognised a signature.** The collector looked for
`sha256-<hex>.sig`. The registry has none: 60 of `kea-dhcp4`'s 71 tags were
signatures, all `sha256-<hex>` with no suffix (D25). So "a signature goes with
its image" had never matched anything, and signatures of deleted images would
have stayed forever. Now both forms are recognised, a signature is only kept
if **the image it signs is kept**, and when one goes, its untagged bundle goes
with it. A signature tag that can't be read stops the run; the old code
skipped it silently.

**What it found.** A dry run on 2026-09-21, after the D25 build:

| | kea-dhcp4 |
|---|---|
| versions | 230 |
| to delete | 118 (51%) |
| — per-architecture wrapper indexes (every build leaves some) | 28 |
| — images, attestations and indexes of 3 builds from before stamped tags | 30 |
| — signatures of those 3 builds (referrer + bundle) | **60** |

The old collector found 54 on the same registry, because it couldn't see the
60 signatures. The other packages matched (118/230; ctrl-agent 59/115).

**Why each build leaves wrappers.** Each architecture is pushed by digest
with its attestations, which makes a small index per architecture. `fuse`
builds the multi-arch index from the *contents* of those wrappers, so the
wrappers themselves are never tagged: 4 per package per build, a few hundred
bytes each, with no layers. Avoiding them would mean giving up buildx's
built-in attestations, so collecting them now and then is the better answer.

**The limit: 25%, with a one-run override.** The old limit was 50%, and the
first correct cleanup was 51%. Raising the default to fit would also let a
broken walk of that size through on every run. Instead, the default drops to
**25%** (in steady state a build leaves 4 wrappers per package among about 26
new versions), and `--max-fraction F` raises it for one deliberate run. `F`
must be below 1, because a limit of 1 or more isn't a limit at all.

**How the tests were checked.** The test suite fakes the network layer
(`urlopen` for the registry, `subprocess.run` for `gh`) rather than
`tag_list()` itself, because the bug was *inside* `tag_list()`: faking it
would have handed the collector a complete list every time. The fake registry
paginates exactly as the real one does above, and every test is judged by the
`DELETE` calls the collector actually made, not by what it printed.

Against the old script, the truncation test **deleted 7 in-use versions**,
and four other tests failed. Then each safety check in the fixed script was
broken in turn (21 variants in all), and each was caught by a named test. The
one that got through makes no real difference: removing the lower bound on
`--max-fraction` only changes an error message, since a limit of 0 already
refuses everything. A control variant that *should* pass (a limit of 0.9,
which the test adapts to) did.

The mutation-testing harness needed fixing twice before we could trust it:

- it counted "no FAIL lines" as a catch without checking the exit code, so a
  run that hung or crashed would have looked like a pass (one did hang)
- the test suite loaded the script through `importlib`, which reuses a
  cached `.pyc` when the file's size and modification second match. Two
  variants of the same length, written within one second, ran the *first*
  variant's code. The suite now compiles from source every time

Three checks also slipped through at first because a *later* check caught
their case: the size limit caught what the tag comparison should have, and
the tag comparison caught what the `Link` parsing should have. Each now has a
test that only it can catch, and the pagination checks are also tested on
`tag_list()` alone.

**Still manual, still a dry run by default.** Nothing runs it on a schedule,
and that's a decision rather than a gap. Every build still leaves 4 tiny
wrapper indexes per package, so there's always *something* to collect, but
they cost nothing (GHCR storage is free, and each is a few hundred bytes with
no layers). A scheduled job would be unattended automation whose worst case
is breaking every published image, in exchange for a tidier version list.
Running it by hand now and then is enough.

We keep the script even so. It's the only tested, safe way to clean up this
registry, and without it the easy option is an off-the-shelf "delete untagged
versions" action, which D17 shows would break all five images.

**The first real run, 2026-09-21**, with no build running and
`--max-fraction 0.55`. `kea-ctrl-agent` first: 65 of 154 deleted, exactly as
predicted (30 signature versions and 21 manifests from the three builds
before stamped tags, plus 14 wrapper indexes). Then checked before going on:
every tag resolved, every manifest they refer to was still there, `cosign
verify` passed on `:3.0` and on its arm64 digest, and `:3.0` pulled. Then the
other four packages: 130 of 308 each (exactly double, as two branches should
be), with the same checks across all five. 585 versions removed in total; a
second run at the default 25% finds nothing in any package.

The post-cleanup check was itself shown to fail first: pointed at a package
that doesn't exist, it reported a problem rather than "0 tags checked, all
good".

---

## D27 — Check every image before tagging any

**Decision.** `fuse` runs in two passes. The first checks every (branch,
image) pair (both architectures present, the stamped tag not already taken)
and writes a plan. The second applies it. Nothing is tagged unless every pair
passed.

**Why.** Tagging already waited until both architectures were pushed, but it
didn't wait across *images*: the checks and the tagging happened in the same
loop. We showed this by running the step from `build.yml` with a stub
`docker`, making the *last* image (`kea-tools`, 3.0) fail its check:

| Last image fails because | Old step | New step |
|---|---|---|
| its arm64 digest is missing | 8 of 9 images retagged, then fails | 0 retagged, fails |
| its stamped tag already exists | 8 of 9 images retagged, then fails | 0 retagged, fails |

Eight images on the new build and one on the old is a version mismatch between
servers that may share a lease database or, with HA, talk to each other. And
nobody would notice, because every image still pulls.

**Narrowed, not closed.** A registry error *during* the second pass can still
stop it part-way. The registry has no way to update several tags as one
transaction, so this is as small as the window gets. Please don't read this
entry as a guarantee.

**The stamp has to stay a `matrix` job output.** The "stamped tag already
exists" error suggests "Re-run all jobs". That advice only works because
re-running all jobs re-runs `matrix`, which makes a fresh stamp, while "Re-run
failed jobs" reuses the old one. A comment next to the stamp now explains
this, because moving the stamp closer to `fuse` looks like a tidy-up, and it
would quietly make that advice wrong.
