# Design decisions

Every significant choice, with the reasoning. Written as a learning record, so
it explains *why* and not just *what*, and it records the options rejected.

Verification date for all upstream facts below: **2026-09-19**.

---

## D1 — Build Kea from source, not from packages

**Decision.** Compile from the ISC source tarball.

**Why.** ISC's own binary packages (apk on Cloudsmith, deb, rpm) are x86_64
only. That is the entire reason this project exists.

**Rejected: Alpine's own `kea` package.** It does not exist. Verified by
pulling `APKINDEX` directly for `edge/community` and `v3.24` on both x86_64
and aarch64 — 22,912 packages, zero named `kea`. The only near-miss is
`kealib`, an unrelated raster-image library for remote sensing. There is no
distro-package shortcut on any architecture.

---

## D2 — Verify with PGP only, not SHA-256

**Decision.** Verify the detached `.asc` signature against a vendored copy of
ISC's code-signing keyblock. Record no SHA-256 in `versions.json`.

**Why.** ISC publishes **no checksum files at all**. Probed and confirmed 404
for `.sha256`, `.sha512`, `SHA256SUMS` and `CHECKSUMS`; the download directory
contains only `kea-X.Y.Z.tar.xz` and `kea-X.Y.Z.tar.xz.asc`.

Any SHA-256 would therefore be one *we* transcribe, and Renovate cannot compute
a hash when it bumps a version — it would leave a stale hash behind and every
build would fail closed until a human pasted the new one. A signature check is
strictly stronger than a self-recorded hash anyway: it proves ISC signed these
bytes, not merely that they match what we saw once.

**The fail-closed property.** The build keyring is created from the vendored
keyblock *only*. A tarball signed by a key outside that block fails with
"No public key" and aborts. This is why we vendor the keyblock rather than
fetching it at build time — fetching it would let an attacker who controls the
download path supply both a tarball and a key to match it.

**Consequence to watch.** The keyblock holds **seven** individual engineer keys
and different releases are signed by different people (3.2.0 by Andrei Pavel,
3.0.4 by Wlodek Wencel). Pinning one fingerprint is impossible. When ISC adds
an eighth key, builds will fail until someone reviews and re-vendors the block.
That is the intended behaviour: trust expansion should be a deliberate act.

### Amendment, 2026-09-20 — the gate was not actually fail-closed

The reasoning above is correct for the case it considered, and the NO_PUBKEY
path was real: a tarball signed from outside the block does fail, with exit 2.
But the implementation trusted `gpg`'s exit code, and that was wrong.

**`gpg --batch --verify` exits 0 for a signature made by a key that has
expired or been revoked.** Both were reproduced against GnuPG 2.4.4:

| Case | Human-readable output | Exit | status-fd token |
|---|---|---|---|
| valid | `Good signature from ...` | 0 | `GOODSIG` |
| expired key | `Good signature ... [expired]` | **0** | `EXPKEYSIG` |
| revoked key | `Good signature ...` + warning | **0** | `REVKEYSIG` |
| bad signature | `BAD signature` | 1 | `BADSIG` |
| signer not in block | `Can't check signature` | 2 | `NO_PUBKEY` |

The revoked case is the dangerous one: the human-readable output still says
**"Good signature"**, and the only thing distinguishing it is a warning line
in a build log nobody reads. A build would have kept passing, and the
fail-closed guarantee would have silently stopped holding.

Note that `GOODSIG` is *absent* in the bad cases rather than accompanied by
something extra. So the fix asserts the **presence of GOODSIG** rather than
the absence of known-bad tokens: a failure mode GnuPG adds in future will also
lack GOODSIG, which keeps the assertion correct without maintenance.

**A premise worth correcting.** The obvious motivation for this — "ISC's keys
will eventually expire" — turns out not to apply. All seven keys in the
current block are set to **never expire** (verified 2026-09-20). The live risk
is therefore *revocation*, not expiry, and specifically this sequence:

1. An ISC engineer's key is compromised and ISC revokes it.
2. We re-vendor the keyblock for an unrelated reason — say, an eighth signer.
3. The new block now contains the revocation certificate.
4. Under the old gate, a tarball signed by the compromised key keeps verifying
   and keeps printing "Good signature".

Re-vendoring is the moment we *learn* about a revocation, and it was also the
moment the old gate would have started ignoring it.

**Why the check moved out of the Dockerfile.** It now lives in
`build/verify-tarball.sh`, called from the builder stage. An inline `RUN`
cannot be tested, and `test/verify-signature-test.sh` drives the real script
against good, expired, revoked, untrusted-signer and tampered-payload
fixtures. Those tests were confirmed to fail against the *old* logic — the
expired and revoked cases were accepted by it — so the gate has now failed for
the right reason before being trusted. It runs in `lint`, in seconds, because
it needs only `gpg` and not a Kea compile.

**Supporting changes.** `build/keys/README.md` records the seven fingerprints,
the date, and the out-of-band check they were confirmed against (the vendored
file is byte-identical to `www.isc.org/docs/isc-keyblock.asc`, a different
host to the one serving the tarballs). `scripts/check-upstream.py` gained a
weekly keyblock check — exit code 3, its own issue title — that reports a
revoked, expired or soon-to-expire key. Since no real key currently expires,
that warning could only ever have reported "healthy", so it too is driven by
revoked and expiring fixtures in the test suite.

### Amendment, 2026-09-20 — the `sha256sum` line was theatre, and is gone

The verification block used to end with:

```dockerfile
sha256sum kea.tar.xz; \
```

It printed a hash and compared it to nothing. **It could not fail.** In the
most security-sensitive block in the build, in a repo whose recurring lesson
is *an assertion that could not fail*, sitting where an auditor reads two
independent checks and there is one.

The substantive position above is unchanged and still correct: a valid
signature over these exact bytes is strictly stronger than matching a hash
fetched from the same server that served the tarball. So this was a fix to an
honesty problem, not a cryptographic one.

**The alternative was checked and is unavailable.** If ISC published a hash
alongside the `.asc` we could fetch and actually compare it. Re-probed
2026-09-20: `.sha256`, `.sha512` and `.SHA256` return 404 for **both** 3.2.0
and 3.0.4, and the directory index lists only the tarball, its signature and
the release notes. So deletion was the only option, and the Dockerfile now
states in a comment that the absence of a hash assertion is deliberate.

The rule this leaves behind: nothing in that block may look like a check and
not be one.

---

## D3 — One image per daemon, not one combined image

**Decision.** Separate images: `kea-dhcp4`, `kea-dhcp6`, `kea-dhcp-ddns`,
`kea-ctrl-agent` (3.0 only), `kea-tools`.

**Why.**

1. **Drop-in replacement.** The goal is that anyone running ISC's images can
   change the registry prefix and nothing else. A combined image breaks that.
2. **Capabilities differ per daemon, and fusing takes the union.** DHCPv4 needs
   `CAP_NET_RAW`; DDNS needs nothing. In one container, DDNS would inherit raw
   socket access for no reason.
3. **Network attachment differs.** DHCPv4/v6 generally need macvlan/ipvlan or
   host networking to see broadcast/multicast. DDNS is happy on a bridge.
4. **One process per container.** A combined image needs supervisord, making
   PID 1 a supervisor and costing the orchestrator per-daemon restart and
   health control. ISC's *Alpine* images deliberately avoid this; only their
   deb/rpm images use supervisord.

**Cost, and why it is small.** All runtime images derive from one shared
`runtime-base` stage, so the registry stores those layers once and a user
pulling all five downloads them once. The marginal cost of each extra image is
one binary. Kea compiles **once** per architecture regardless — `ninja install`
produces every daemon from a single build — so the split costs nothing in
build time.

---

## D4 — Monorepo, not repo-per-image

**Decision.** One repository publishing many images.

**Why.** Repo-per-image makes the shared builder stage impossible, so Kea would
compile five times per architecture instead of once — ten full compiles per
release. It also forces five copies of `versions.json`, five Renovate configs,
and turns "adding a branch is a one-line edit" into a five-repo change.

GHCR packages are owned by the account, not the repo, and are linked back by
the `org.opencontainers.image.source` label — so one repo publishes
`ghcr.io/dapalab/kea-dhcp4` and friends without difficulty.

---

## D5 — `kea-tools` as a published image

**Decision.** Publish a fifth image carrying `perfdhcp`, `kea-admin`,
`keactrl` and `kea-lfc`. **`kea-shell` is deliberately excluded.**

**Why.** A load generator does not belong inside a production DHCP server
image, but CI needs `perfdhcp` to prove a real handshake. Splitting it out
solves that and closes a real gap in ISC's images: their issue #46 is
"database schema update impossible because kea-admin missing in docker images".

**Why not `kea-shell`.** It is a standalone Python client for the control API.
Including it requires `python3`, which measured at **122 MB -> 65 MB** when
removed: more than half the image existed to support one convenience wrapper.
Verified that nothing depends on it — no binary or library references it, the
HA hook `libdhcp_ha.so` has no Python dependency and lives in the daemon images
anyway, and DDNS is daemon-to-daemon over UDP. The full 33-command control API
is reachable with the `nc` already present in every image for the healthcheck,
so this removes a wrapper, not a capability.

---

## D6 — Per-branch image list in `versions.json`

**Decision.** Each branch entry carries its own `images` array.

**Why.** `kea-ctrl-agent` exists in 3.0 (`src/bin/agent`) but was **removed in
3.2** once the daemons gained native HTTP/TLS control sockets. Verified by
extracting both tarballs. The daemon set is therefore a property of the branch,
not a global constant. Retiring the agent when 3.0 reaches EOL becomes a
one-line edit.

---

## D7 — Explicitly pin every optional dependency

**Decision.** Pass `-D mysql=disabled -D postgresql=disabled -D netconf=disabled
-D krb5=disabled` rather than relying on defaults.

**Why.** Those four options are Meson `feature` type with **no declared
default**, which means `auto`. An `auto` feature silently enables itself if its
dependency happens to be present. Left alone, the contents of the image would
depend on which `-dev` packages the builder stage happened to install — a
reproducibility hazard that is invisible until it bites.

**Known deviation, deliberately kept.** ISC's `kea-dhcp-ddns` image installs
`isc-kea-gss-tsig`. We disable `krb5`, so our DDNS image has no GSS-TSIG hook.

This was initially flagged for v2 as a functional gap. On investigation it is
not one for the intended audience:

- **GSS-TSIG** (RFC 3645) is Kerberos/GSSAPI-based and is used mainly with
  Active Directory DNS. It requires a KDC, a `krb5.conf` and a keytab.
- **TSIG** (RFC 2845) is the shared-HMAC-key mechanism BIND9, NSD, Knot and
  PowerDNS use. It is **compiled into `kea-dhcp-ddns`**, not a hook, and works
  in our image today — verified by validating a full forward+reverse TSIG
  configuration against it.

Cost to enable GSS-TSIG if ever wanted, `kea-dhcp-ddns` only: `krb5-dev`
706 KiB at build, `krb5-libs` 1.7 MB (amd64) / 2.3 MB (arm64) at runtime, plus
one meson flag. Cheap, but pointless for a BIND9 deployment.

---

## D8 — Non-root with file capabilities

**Decision.** Every image runs as `kea` (UID/GID 10000). `kea-dhcp4` and
`perfdhcp` carry file capabilities set with `setcap`.

**Why.** ISC's images run everything as root. Running as non-root is the
obvious improvement, but there is a subtlety worth recording: **in Docker,
`--cap-add` does not grant a capability to a non-root process.** Added
capabilities land in the bounding and permitted sets of the *container*, but a
non-root process only acquires them if the binary carries file capabilities or
the runtime supports ambient capabilities — which Docker does not expose.

So `setcap cap_net_raw,cap_net_bind_service=+ep` on the binary is what actually
makes non-root DHCPv4 work. The capabilities are in Docker's default bounding
set, so no `--cap-add` is needed; but `--cap-drop=ALL` *will* break it, and
that is documented in the README.

**Minimum sets.**

| Image | Capabilities |
|---|---|
| `kea-dhcp4` | `CAP_NET_RAW`, `CAP_NET_BIND_SERVICE` |
| `kea-dhcp6` | `CAP_NET_BIND_SERVICE` |
| `kea-dhcp-ddns` | none |
| `kea-ctrl-agent` | none |
| `kea-tools` | `CAP_NET_RAW` (perfdhcp only) |

---

## D9 — Ship a commented template as the default config

**Decision.** The default config baked into each image is a heavily commented
template. It defines no subnets and no DDNS domains, so the daemon starts, logs
and answers its control socket but serves nothing.

**Why a template rather than a bare minimal config.** An empty config is safe
but teaches nothing — it gives you no idea how to express a pool, a
reservation, or DDNS wiring, which is most of what someone setting this up
actually needs. The template is the same file in the repository
(`build/config/`) and in the image, so there is one artifact to maintain and it
is reachable either by browsing the repo or by `cat`-ing it out of an image you
have already pulled.

**Verified, not assumed.** The example subnet block was mechanically
uncommented, retargeted at a test network and run: 9/9 REQUEST-ACK with leases
allocated from the pool. A template whose example is subtly wrong would be
worse than no template at all.

**Why this deviates from ISC.** ISC's images ship a live `192.168.50.0/24` pool
bound to `eth0`. A DHCP server that starts already willing to hand out
addresses is a poor default for an image people are told to run on macvlan —
the failure mode is answering DHCP on someone else's network.

**We also do not ship `hiddens`.** ISC's v3_2 images contain a password file
with the literal contents `api-user-name:api-user-password`, wired into the
default config's HTTP basic-auth block. Shipping working credentials inside a
public image is indefensible; our default exposes the unix control socket only.

---

## D10 — Tagging scheme

**Decision.** `3.2.0`, `3.2`, `3.0`, `3.0-lts`. No `latest`. **No bare `3`.**

**Why no bare `3`.** It would resolve to 3.2.0 today. But 3.0 is the LTS and
its EOL (Jun 2028) is **eleven months later** than 3.2's (Jul 2027). A user
pinning `3` for stability would get the shorter-lived branch — the opposite of
their intent. `3` communicates nothing `3.2` does not, and misleads.

**Why no `latest`.** A silently moving DHCP server major version is a bad idea.
Note ISC does publish `latest`, currently pointing at 3.2.0 while 3.0 is the
LTS — exactly the trap described above.

### Amendment, 2026-09-20 — `3.2.0` was a moving tag pretending not to be

The reasoning above is unchanged and stays. What it missed is that the scheme
had **no immutable tag at all**.

The weekly rebuild (D14) re-pushes every tag with different bytes, by design —
that is how Alpine security fixes reach users between Kea releases. So
`3.2.0`, which reads as a pinned version, moves every Monday. The README
stated outright that it did not.

Two consequences, neither hypothetical:

1. Pinning `3.2.0` for reproducibility does not give reproducibility.
2. A cosign signature is bound to bytes. After a rebuild, the signature on
   last week's index is still valid, but `3.2.0` no longer resolves to it —
   so the supply-chain story in the README implied a guarantee the rebuild
   schedule quietly broke.

**Decision.** Add a stamped tag that is never reused:

```
3.2.0-20260920-2203   immutable
3.2.0                 moves with each rebuild
3.2                   moves
3.0-lts               moves
```

This follows the pattern ISC already uses on Cloudsmith (`3.1.9-20260527`):
an immutable tag with the bare version moving on top. It extends D10 rather
than replacing it — no `latest` and no bare `3` still hold.

**Why the time, and not just the date as ISC does.** ISC releases rarely, so
a date is unique for them. Here it is not. Images rebuild weekly *and* on
every automerged Renovate PR, because an Alpine digest or grouped-actions
merge is a push to `main` and therefore a build. **Four builds ran on
2026-09-20 alone.** A date-only stamp would have published
`3.2.0-20260920` four times over different bytes — precisely the property
the tag exists to provide, inverted. The first attempt at this shipped with
date-only granularity and was corrected the same evening.

Minute resolution is enough because `concurrency: build-<ref>` serialises
runs on `main` (no `cancel-in-progress` outside pull requests) and a build
takes 17+ minutes, so two `fuse` jobs cannot tag in the same minute. That is
an argument, though, and this project has twice been caught trusting one, so
`fuse` also queries the registry and **fails if the immutable tag already
exists**. Overwriting a tag documented as never reused is worse than failing
a publish. Verified against the live registry: the check reports an existing
tag as present and an unused one as free.

One consequence to know about: after "Re-run failed jobs", the stamp is
carried over from the original attempt, so the check will correctly refuse.
"Re-run all jobs" re-runs the `matrix` job and stamps a fresh time. The error
message says so.

**Rejected: stop re-pushing the version tags.** That would mean a user on
`3.2.0` never receives an Alpine security fix, which inverts the point of the
weekly rebuild. Moving tags are correct here; what was missing was somewhere
to stand still.

**Rejected: tell people to use digests and leave it.** Digests are the
strongest option and the README still offers them, but a 71-character digest
in a Compose file is something people avoid, and a pin nobody adopts protects
nobody. A readable immutable tag is the one that gets used.

### The stamped tag is invisible to Renovate — deliberately, and documented

Raised during review: would a downstream consumer's Renovate still track
version updates through a stamped tag? **No.** Verified by running the tag
shapes through Renovate's own `docker` versioning module rather than
reasoning about it:

| Pinned on | Renovate offers |
|---|---|
| `3.2.0` | `3.2.1` |
| `3.2` | `3.4` |
| `3.2.0-20260920` | **nothing** |
| `3.2.0-20260920-2203` | **nothing** |

`docker` versioning treats everything after the first `-` as a
*compatibility* string and only offers upgrades sharing it;
`isCompatible("3.2.1-20261001-0417", "3.2.0-20260920-2203")` is `false`. It
fails **silently** — no error, just permanent quiet.

Note this is a property of stamping at all, not of the added time: date-only
is equally invisible. So the precision fix costs nothing here.

**This does not argue against the tag, because the footgun is not unique to
it.** Any immutable pin freezes you; a digest pin freezes you just as hard.
The real distinction is narrower: Renovate *can* bump a digest pin and
*cannot* bump a stamped tag. So the two serve different people, and the
README now routes by audience rather than leaving readers to guess:

- **automated consumers** — a moving tag plus `pinDigests`. Reproducible per
  deployment, and the bot raises a PR on each rebuild (Alpine fixes) and on
  each version bump. Strictly better for them than a stamped tag.
- **manual pinners** — the stamped tag, with the trade stated plainly: it is
  readable and permanent, and nothing will ever prompt you to move off it.

For anyone who insists on automating against stamped tags, `regex`
versioning does work, and was verified to: pinned on `3.2.0-20260920-2203`
it offers both `3.2.0-20261001-0417` and `3.2.1-20261115-0417`. The README
carries the snippet **with `allowedVersions`**, because without it the same
config offers `3.0.4-…` → `3.2.0-…` and walks the user off the LTS branch
onto the shorter-lived stable one.

### Two legacy date-only tags exist, and stay

The first run under this scheme published `3.2.0-20260920` and
`3.0.4-20260920` before the granularity was corrected. They stay, for a
reason worth recording: **GHCR has no tag-level delete.** Removing a tag
means deleting the package *version*, and that version is the same index
`3.2.0`, `3.2`, `3.0` and `3.0-lts` point at — deleting it would break every
current tag. That is D17's untagged-children trap wearing a different hat.

They are harmless. Once the format changed they can never be overwritten, so
they remain permanently immutable and honest: two one-off tags from the first
run of the scheme.

**Where the date comes from.** The `matrix` job stamps one timestamp for the
whole run and both the `created` label and the date tag derive from it
(D21). Deriving the tag with a fresh `date` in `fuse` would let a run that
crosses midnight UTC label an image one day and tag it the next. It is also
why the per-arch `CREATED` stamping introduced with D21's first fix was
consolidated: amd64 and arm64 were stamping separately, minutes apart.

**Guarded.** `matrix.py check` now asserts the composition of the tag list
directly — exactly one immutable stamped tag, in first position, no
`latest`, no bare major, no duplicates — and that guard was confirmed to fire
before being trusted. A
published tag cannot be recalled, so this belongs in `lint` rather than in
review.

---

## D11 — Native runners, not QEMU

**Decision.** amd64 on `ubuntu-24.04`, arm64 on `ubuntu-24.04-arm`, fused with
`docker buildx imagetools create`.

**Why.** Kea is a large C++ codebase. Measured on the development machine (a
4-core Intel N97) a native amd64 compile runs ~77 minutes. QEMU-emulated
aarch64 typically costs 5-10x, which would approach the 6-hour job ceiling with
no upside. GitHub's arm64 hosted runners are free on public repositories.

---

## D12 — MPL-2.0, and what we owe upstream

**Decision.** License this repository MPL-2.0. Do not copy from `kea-docker`.

**Why.** `kea-docker` carries no LICENSE file on any branch — only
`SPDX-License-Identifier: MPL-2.0` headers on its three Dockerfiles. MPL-2.0 is
file-level copyleft: copy a file and that file stays MPL-2.0 with its notice
intact, but it does not affect the rest of the repository.

We copy nothing. There is no entrypoint script to derive from (ISC's Alpine
images have none — just `CMD`), and the structure we reuse is `FROM` / `VOLUME`
/ `EXPOSE` / `CMD`, which are interface facts about how Kea runs rather than
creative expression. We adopt the *conventions* — paths, ports, volume layout —
and write our own files. MPL-2.0 headers go on ours anyway, which costs nothing
and removes the question.

**Separately:** Kea itself is MPL-2.0 and we redistribute its binaries, so
`COPYING` and `AUTHORS` ship inside every image at
`/usr/share/licenses/kea/`.

---

## D13 — Database backends: enabled

**Decision.** MySQL and PostgreSQL backends are compiled in. `kea-tools` ships
`kea-admin` together with the `mysql` and `psql` clients it shells out to, and
the schema SQL.

**Revised from the original non-goal**, which read "no MySQL/PostgreSQL support
in v1 *unless it is nearly free to add*". It is: measured below. The condition
was met and should have been acted on the first time it was measured.

**Cost of adding them later, measured from Alpine 3.24 `APKINDEX`:**

| | Build stage | Runtime stage |
|---|---|---|
| MySQL | `mariadb-connector-c-dev` 244 KiB | `mariadb-connector-c` 691 KiB |
| PostgreSQL | `libpq-dev` 1.87 MiB | `libpq` 386 KiB |

About **+1 MB on the runtime image** and two Meson flags. Kea discovers both
purely via pkg-config (`dependency('mariadb')`, `dependency('libpq')`), so
there is no `pg_config` hunting.

The daemons link `libmariadb` and `libpq` **directly** - they never invoke a
CLI client. So the daemon images need no client tooling at all, and a build
report showing `MySQL: yes` plus a config using `type: mysql` surviving
`kea-dhcp4 -t` is sufficient proof the backend is compiled in and registered.

### Why `kea-tools` carries the clients

`kea-admin` shells out to `mysql` (30 call sites) and `psql` (5). Shipping it
without them would make it a trap: present, apparently usable, unable to
perform its primary function.

Installing the full packages costs **65.6 MB**, because `mariadb-client` is
seven near-identical ~5 MB binaries (`mariadb-dump`, `mariadb-check`,
`mariadb-import`, ...) that `kea-admin` never calls. Copying just the two
binaries it does call costs **8.8 MB**, measured against an image mirroring
`runtime-base`:

| | Image | Marginal |
|---|---|---|
| runtime-base equivalent | 20.8 MB | — |
| **+ `mariadb` and `psql` binaries** | 29.6 MB | **+8.8 MB** |
| + full client packages | 86.4 MB | +65.6 MB |

Their other dependencies - `libcrypto`, `libssl`, `libstdc++`, `libz`,
`libgcc` - are already in `runtime-base`, so they add nothing. `mysql` is a
symlink to `mariadb`; the deprecation notice it prints goes to **stderr only**,
verified, so `kea-admin`'s stdout parsing is unaffected.

### The size question was the wrong question

`kea-tools` is not deployed. It is a `docker run --rm` for a one-off task, on a
machine that is not serving DHCP, and it shares `runtime-base` with the daemon
images a user already has - so its real cost is the delta over layers already
on disk, paid once. The cherry-pick is kept because it is free, not because the
size mattered.

---

## Phase 1 measured results

Built and tested on the development machine (Intel N97, 4 cores, 5.7 GB RAM,
WSL2) for `linux/amd64`, Kea 3.2.0 on Alpine 3.24.

### Build

| | |
|---|---|
| Kea compile (652 objects, `-j3`) | **51 min** |
| Each runtime image after that | **2-10 s** |

The compile is cached as a single builder stage, so building all four images
costs one compile — confirmed by `#13 CACHED` on every subsequent target. This
is the measurement that justifies D3 and D4: fanning out per image or per
repository would have cost four compiles instead of one.

### Image sizes

| Image | Reported | Notes |
|---|---|---|
| `kea-dhcp-ddns` | 58 MB | |
| `kea-dhcp6` | 68 MB | |
| `kea-dhcp4` | 68 MB | |
| `kea-tools` | 65 MB | was 122 MB; `kea-shell` and its `python3` dependency removed |

Transfer size tells the more useful story:

| | |
|---|---|
| `kea-dhcp4` alone | 18.9 MB |
| All four together | **27.4 MB** |

Four layers are shared by all four images (Alpine base, runtime packages, the
`libkea-*` stack, licences). Pulling all four therefore costs roughly 1.5x one
image rather than 4x — the marginal cost of each extra image is its binary,
plus `python3` in the case of `kea-tools`.

### Two bugs the tests caught

Both are recorded because they are the kind of thing that would otherwise have
surfaced in production.

**1. Kea 3.x rejects a control-socket directory more permissive than 0750.**
`mkdir -p` leaves 0755, and Kea refuses it at config-parse time with
`socket path:/run/kea does not exist or has more relaxed permissions than 750`.
This is the same security-policy change behind ISC's kea-docker#45. Fixed by an
explicit `chmod 0750` on `/run/kea` and `/var/lib/kea`.

**2. `set -o pipefail` plus `grep -q` is a latent race.** `grep -q` exits at the
first match, `docker logs` upstream takes SIGPIPE, and pipefail reports the
pipeline as failed even though the match succeeded. The readiness check passed
only by luck and would have been flaky in CI. Logs are now captured to a
variable and matched with `case`.

### Claims verified, not assumed

| Claim | Result |
|---|---|
| Daemons run as non-root | `uid=10000(kea)` |
| File capabilities present | `cap_net_bind_service,cap_net_raw=ep` |
| Healthcheck reports healthy | all three daemon images |
| `--cap-drop=ALL` breaks dhcp4 | `exec: operation not permitted` |
| Re-adding both capabilities works | starts normally as UID 10000 |
| `CAP_NET_RAW` alone is insufficient | fails - **both** are genuinely required |
| DHCPv4 DORA completes | 49/49 REQUEST-ACK, ~0.7 ms average |

The capability failure mode is worth noting: a binary carrying file
capabilities that are absent from the bounding set fails at `exec` with
`operation not permitted`. It is a loud failure rather than a silent
degradation to a daemon that cannot bind, which is the better outcome.

### Known limitation

The functional test forces `dhcp-socket-type: udp` so that perfdhcp can unicast
to the server on a bridge network. It therefore does **not** exercise the raw
socket path that a real macvlan deployment uses. Testing raw sockets requires
an L2 segment with broadcast, which is not reproducible in a standard CI
sandbox. The `CAP_NET_RAW` grant is verified separately (see above), but the
broadcast path itself is not covered.

---

## D14 — `install_umask=0022` (found the hard way)

**Decision.** Pass `-D install_umask=0022` to `meson setup`.

**Why.** Kea **3.0.x** sets `'install_umask=0027'` in its `project()`
`default_options`. That installs every library as `0750 root:root`. Our daemons
run as UID 10000, so they cannot read their own libraries and die at startup
with:

```
Error loading shared library libkea-util.so.103: Permission denied
Error relocating /usr/sbin/kea-dhcp4: ... symbol not found
```

The relocation errors are a red herring — they are downstream of the loader
failing to open the libraries at all.

**Kea 3.2 removed that global umask**, replacing it with explicit
`install_mode: 'rwxr-x---'` on the state directories only. So 3.2 installs
0755 and works, while 3.0 does not. This is why the CI matrix failed on exactly
one branch, on both architectures.

**ISC never hits this** because their images run everything as root. It is a
direct consequence of D8 (non-root), and a good example of why the two stable
branches cannot be assumed to build identically.

**Verified rather than assumed**, with a minimal Meson project:

| | installed mode |
|---|---|
| `install_umask=0027` | `750` |
| `install_umask=0022` | `755` |
| `project()` says `0027`, CLI passes `0022` | **`755`** — CLI wins |

That last row is the one that matters: Kea sets the value in `default_options`,
and a command-line `-D` overrides it.

Setting it explicitly for both branches is deliberate. It makes the permissions
an intentional property of our build rather than something inherited from
whichever upstream branch we happen to be compiling.

---

## D15 — OCI image labels

**Decision.** Every image carries the standard `org.opencontainers.image.*`
labels, with `source` pointing at this repository.

**Why it matters beyond tidiness.** `org.opencontainers.image.source` is what
links a published GHCR package back to its repository. An unlinked package does
not inherit the repository's permission model, and the first publish attempt
failed with:

```
denied: permission_denied: write_package
```

despite the job being granted `Packages: write` and `docker login` succeeding.
We had set **no labels at all**.

**The disclaimer travels with the image.** `vendor` and `description` both state
that these are unofficial community builds not affiliated with ISC, so
`docker inspect` carries the disclaimer even for someone who never reads the
README. That is the right place for it: an image can outlive the context in
which it was found.

**Dynamic values** (`version`, `revision`, `created`) come from build args the
workflow supplies from `matrix.kea`, `github.sha` and the build timestamp, so
every published image records exactly which commit and which Kea release
produced it.

---

## D16 — Renovate runs self-hosted, as a GitHub App

**Decision.** Renovate runs from `.github/workflows/renovate.yml` using a
GitHub App token, not `GITHUB_TOKEN` and not the hosted Renovate app.

**Why self-hosted.** No third-party service needs access to the repository,
and the schedule sits beside the other two clocks (weekly rebuild, upstream
watcher) where all three can be seen together.

**Why an App rather than `GITHUB_TOKEN`.** This is a practical constraint, not
a preference: **pull requests opened with `GITHUB_TOKEN` do not trigger other
workflows.** GitHub does this to prevent recursion. A Renovate PR bumping Kea
would therefore never have `build.yml` run against it, so the update would
arrive unvalidated - defeating the purpose of proposing it. An App token is
not `GITHUB_TOKEN`, so the restriction does not apply.

**Why an App rather than a PAT.** A PAT would also trigger workflows. The App
wins on three counts: tokens are minted per run and expire within the hour, so
only a private key is stored; PRs come from a bot identity rather than a human
account; and it is scoped to exactly this repository. The honest tradeoff is
that a long-lived private key is still a stored secret - better scoped and
independently revocable, not absent.

**The App needs `Workflows: write`**, which is easy to miss. Renovate edits
`.github/workflows/*.yml` to bump SHA-pinned actions, and without that
permission those PRs fail with a confusing push rejection.

### Update policy, and why the three differ

| | Automerge | Reasoning |
|---|---|---|
| Kea version | **never** | A DHCP server version bump is a human decision, however green CI is. 3-day minimum release age. |
| Alpine **digest** | yes | This is how Alpine security fixes reach the images between releases. |
| Alpine **tag** (3.24 → 3.25) | no | A new Alpine release can move compiler and library versions underneath the build. 7-day age. |
| GitHub Actions | yes | Grouped, SHA-pinned, 3-day age. |
| **Runner images** (`ubuntu-24.04`) | **no** | Renovate's github-actions manager also tracks `runs-on:`. A dry run caught it grouping these in with action bumps, where they would have been automerged - silently changing the OS the build runs on. 30-day age. |

### Per-branch constraints

Both `versions.json` entries resolve to the same upstream repository, so they
would collide under one dependency name. The custom manager synthesises
distinct names from the branch key - `kea-3.0`, `kea-3.2` - while
`packageNameTemplate` carries the real lookup target.

`allowedVersions` then constrains each to its own minor: `>=3.0.0 <3.1.0` and
`>=3.2.0 <3.3.0`. That is what stops the LTS entry being offered 3.2.x, and it
**structurally prevents an odd-numbered development release** ever being
proposed, since 3.1 and 3.3 fall outside both ranges. A future 3.4 is excluded
too, which is correct: adopting a new stable branch is a human decision, and
the upstream watcher opens an issue when one appears.

Verified against the real files: the manager produces exactly two Kea
dependencies with the right names and values, two `alpineRef` matches, and one
Dockerfile `ARG` match.

---

## D17 — Untagged package versions are NOT garbage (deferred cleanup)

**Decision.** No automated package cleanup for now. Recorded here because the
obvious implementation destroys every published image.

**The trap.** A multi-arch tag points at an OCI **index**. Only the index
carries the tag; the per-architecture manifests and the SBOM/provenance
attestations beneath it are untagged by design. Measured on the live packages:

```
kea-dhcp4:3.2  ->  sha256:2396fd43...  amd64/linux    tagcount=0
                   sha256:90919455...  arm64/linux    tagcount=0
                   sha256:ef5e6d2b...  attestation    tagcount=0
                   sha256:b1e74fe2...  attestation    tagcount=0
```

Every child of a working tag is untagged. So the usual "delete all untagged
versions" retention action - the first thing anyone reaches for - would delete
the amd64 and arm64 manifests out from under `kea-dhcp4:3.2`. The tag would
survive, pointing at an index whose children are gone, and every pull would
fail in a way that looks like registry corruption.

**If cleanup is ever implemented**, the keep-set must be computed by
**reachability**, not by tag presence:

1. list every tag on the package
2. resolve each to its index and collect all referenced child digests
3. keep = tagged versions **plus everything they reference**
4. delete only what is outside that set **and** older than a grace period

**The grace period is a promise, not a nicety.** The README tells people to pin
digests for anything they care about. Deleting an old digest breaks exactly the
users who followed that advice. 90 days means a pin survives a quarter.

**Why deferred.** Public GHCR storage is free, so the cost is navigational
clutter rather than money. Accumulation runs at roughly 7 versions per package
per weekly rebuild - about 360 a year each, driven by the rebuild schedule
rather than by releases. Tolerable for a year or two.

---

### Amendment, 2026-09-20 — measured, and a safe collector written

The deferral stands on the numbers. Measured before any cleanup existed:

| Package | Versions | "Delete untagged" would remove | ...of which were LIVE children |
|---|---|---|---|
| `kea-dhcp4` | 136 | 94 | **48** |
| `kea-ctrl-agent` | 68 | 47 | **24** |

**Roughly half of every untagged version is load-bearing** — a per-arch
manifest or an SBOM/provenance attestation under a working index. The
default setting of essentially every off-the-shelf GHCR cleanup action
would have broken all five images at once.

`scripts/gc-packages.py` computes reachability instead: keep anything
tagged, anything an index points at, and any signature whose subject
survives. Everything else is the remains of a build whose tags have moved
on.

**A subtlety not in the original entry.** 40 of `kea-dhcp4`'s 47 registry
tags are cosign `sha256-<digest>.sig` / attestation tags. Deleting a
superseded index without its signature leaves a tagged signature pointing
at nothing, so signatures are paired with their subject and share its fate.

**Three refusals, each tested by breaking the thing it guards:**

| Guard | Proved by |
|---|---|
| Refuse if >50% of a package would go | Disabling the reachability walk: it stopped at 47/68 rather than deleting the package |
| Refuse if the registry cannot be walked | A bad owner: `403`, exit 1, nothing deleted |
| Refuse while a build is in flight | Running it during a live build: `fuse` pushes an index *then* tags it, so a walk landing between would read a live index as garbage |

Dry run is the default and deletion needs `--delete`.

**Validated against all five live packages** with an independent
reachability walk that does not reuse the collector's own result: of the
207 versions it proposes deleting across the five, **zero** are reachable
from any tag.

Still not scheduled, and that is deliberate. It runs by hand, and should
keep running by hand until it has been used enough times to be boring.
## D18 — Keyless cosign signing

**Decision.** Every published image is signed with cosign using the workflow's
OIDC identity. No key material is stored anywhere.

**Why keyless.** A signing key held in a repository secret has to be
generated, stored, rotated and protected, and its compromise is silent. Keyless
signing binds the signature to the workflow identity
(`.../build.yml@refs/heads/main`) and records it in Sigstore's public
transparency log. There is nothing to leak.

**Sign the index, not the tags.** One signature per image index covers every
tag pointing at it, so `3.2` and `3.2.0` need one between them. It also stays
valid when a moving tag is later repointed, because the signature is bound to
bytes rather than to a name.

**Signing happens last**, after the multi-arch verification step. A signature
asserts "this CI produced these bytes"; attaching one to an image we had not
yet confirmed was correctly assembled would make the claim misleading. The
workflow then verifies its own signatures before finishing, so a run cannot
report success having produced something unverifiable.

**The chain this completes.** ISC signs the source tarball → the build verifies
that signature against a vendored keyblock and fails closed → CI signs the
resulting image → a user verifies that signature pins it to this repository.
Each link is checkable by someone who trusts none of the others.

**What it does not claim.** Only origin and integrity. It says nothing about
ISC endorsing these images - which is precisely the point, since the useful
question about an unofficial image is *who actually built this*.

---

## D19 — No DHCPv6 functional test (known gap)

**Decision.** `kea-dhcp6` is covered by gate 5 (starts, control socket answers)
but there is no automated SARR exchange test. This is deliberate.

**Why it was attempted and abandoned.** DHCPv6 is architecturally
multicast-based: clients send SOLICIT to `ff02::1:2` from a link-local address
on a real L2 segment. Docker's bridge IPv6 does not reproduce that. A prototype
showed two distinct problems:

1. **Kea failed to bind its link-local multicast socket** on startup -
   `Failed to bind socket to fe80::.../port=547: Address not available`. This
   is a duplicate-address-detection race: the container's link-local address is
   still tentative when Kea starts. A 5-second delay before starting Kea makes
   it go away, which confirms the diagnosis.
2. **Even with sockets open, perfdhcp received nothing** - 20 SOLICIT sent, 0
   ADVERTISE received, because the unicast coercion needed to avoid multicast
   does not match how Kea expects to be addressed.

Pursuing it further would mean a test carrying a `sleep` for DAD plus
increasingly artificial addressing. At that point it exercises **our
workarounds**, not the image.

**This is the same wall as the v4 raw-socket path.** The v4 functional gate
uses `dhcp-socket-type: udp` because a Docker bridge has no broadcast. Accepting
the equivalent limit for v6 is consistent with that, not a lower standard.

**What is covered regardless.** Gate 5 proves the daemon starts with its
shipped config and answers its control socket. The v4 DORA test exercises the
shared `libkea` stack - allocation engine, lease manager, configuration parsing
- which v6 uses too. What remains uncovered is the v6 packet layer specifically.

**Where it should be validated: real hardware**, on an L2 segment, exactly as
the v4 raw-socket path was validated on a Raspberry Pi. Documented here rather
than hidden so anyone relying on the v6 image knows precisely what CI does and
does not prove.

---

## D20 — Branch protection, so automerge is enforced rather than trusted

**Decision.** `main` requires a status check named `required` to pass. Native
auto-merge and branch deletion are enabled; Renovate uses platform auto-merge.

**Why.** Renovate automerges Alpine digest and GitHub Actions updates. Without
required checks, nothing *structurally* prevented a bad merge - Renovate checks
CI status before merging, but that is Renovate policing itself. If a workflow
failed to trigger, it would see no failing checks and merge. For a repository
that publishes DHCP server images unattended, that enforcement should come from
the platform.

### The trap: matrix job names are not stable

Build job check names embed the versions:

```
build (3.2, 3.2.0, alpine:3.24@sha256:294b68...)
```

Those change whenever `versions.json` changes - exactly what Renovate does.
Requiring them directly would leave branch protection waiting forever for a
check that never reports under its old name. **The first Kea bump would have
deadlocked itself**, blocking the update the automation exists to deliver.

Hence the `required` job: depends on `lint`, `matrix` and `build`, runs with
`if: always()`, and fails if any of them failed, was cancelled or was skipped.
Its name never changes.

### Settings, and why each

| Setting | Value | Reasoning |
|---|---|---|
| `checks` | `required` only | stable name; see above |
| `strict` | **false** | `true` forces every PR to be rebased whenever `main` moves. On a repo where Renovate opens several PRs at once that is constant churn for no safety gain here. |
| `enforce_admins` | **false** | Keeps an emergency escape hatch for the owner. It does **not** weaken the Renovate guarantee: Renovate acts through a GitHub App, not as an admin, so it is bound by the check regardless. |
| `required_pull_request_reviews` | none | Solo project. Requiring a review you would give yourself is theatre, and it would block automerge entirely. |
| `allow_force_pushes` | false | Protects history and the commit signatures on it. |
| `allow_deletions` | false | `main` cannot be deleted. |

**The practical effect:** Renovate cannot merge anything unless `required`
passes. The owner retains direct push as a deliberate exception, which is the
right split - the automation is constrained, the human is not.

---

## D21 — Prove the published image is the tested image, don't infer it

**Decision.** Compute the build timestamp once per job and reuse it for both
buildx invocations, then assert after pushing that the image manifest
published is byte-identical to the one the gates ran against.

**The defect.** `CREATED` was computed separately in the test step and the
push step, minutes apart with the smoke test in between. It feeds
`org.opencontainers.image.created`, so the label layer differed, so the two
builds produced different images. The workflow's claim — tests run before
anything is pushed — was literally true, while the stronger thing a reader
takes from it, *we shipped the bytes we tested*, was not.

In practice the delta was one timestamp string. The tail case is what matters:
if the GHA cache were evicted or missed during a long smoke test, the push
build recompiles Kea from scratch and publishes a genuinely different build,
and nothing would have noticed.

**Why the obvious assertion does not work.** Comparing the two
`containerimage.digest` values from the metadata files fails *permanently*,
for a reason that is easy to assume away. They are not the same kind of
object:

| Build | What `containerimage.digest` reports |
|---|---|
| `--load` (test) | the OCI **image manifest** digest |
| registry push | the **index** digest |

A registry push wraps the image manifest in an index alongside the
provenance and SBOM attestation manifests, which carry platform
`unknown/unknown`. Verified empirically against a local registry: the index
digest differs depending on whether attestations are enabled, while the child
image manifest is identical in both cases and equal to the `--load` digest.

So the review's premise — attestations are separate manifests and therefore
do not affect the image digest — is right about the image and wrong about the
*reported* digest. The check compares like with like: the index's own image
manifest child, selected by `platform.os != "unknown"`.

**Validated by simulation** against a local registry, three cases:

| Case | Result |
|---|---|
| `CREATED` identical (the fix) | digests match, publish proceeds |
| `CREATED` differs (the old bug) | assertion fires |
| cache miss, different content | assertion fires |

So the check has failed for the right reason, twice, before being trusted.

**Accepted trade-off.** A false positive blocks publishing rather than
shipping untested bytes, which is the correct direction for this failure to
point. If a legitimate cache miss ever trips it, the fix is to rerun the job,
not to weaken the assertion.

---

## D22 — Gate kea-lfc on compaction, not on the binary being present

**Decision.** A sixth smoke gate runs `kea-dhcp4` with `lfc-interval: 5`,
drives leases through it, and asserts the lease file was actually compacted —
with a negative control that replaces `kea-lfc` with a stub that fails.

**The gap.** `kea-lfc` ships in `dhcp4`, `dhcp6` and `tools`, and both shipped
templates schedule it hourly, but nothing ever ran it. Same shape as the
`kea-ctrl-agent` bug: present, configured, plausible, never executed. At
`lfc-interval: 3600` it would not fire inside a CI run anyway.

**The review's stated failure mode turned out to be wrong**, which changed the
test. It expected that a `kea-lfc` which cannot be found "fails silently".
Measured: it fails *loudly*, at startup, before serving anything —

```
DHCPSRV_MEMFILE_FAILED_TO_OPEN Could not open lease file:
    File not found: /usr/sbin/kea-lfc
DHCP4_INIT_FAIL failed to initialize Kea server
```

Gates 4 and 5 already cover that, because the daemon never reaches
`DHCP4_STARTED`. Testing for a missing binary would have been another
assertion that could not fail.

**What does fail silently is a `kea-lfc` that is present and broken** — a
missing shared library, a wrong-architecture binary, a directory it cannot
write. Kea execs it and **never checks the exit status**. Measured against a
stub that exits 1:

| | working `kea-lfc` | broken `kea-lfc` |
|---|---|---|
| container | healthy | **healthy** |
| logs | `LFC_START` / `LFC_EXECUTE` | **identical, no error** |
| `kea-leases4.csv.2` (compacted) | present, one row per address | **absent** |
| `kea-leases4.csv.1` left behind | no | **yes** |
| stale `.pid` | no | **yes** |

Nothing surfaces until the lease file has grown for weeks and restart times
degrade.

**So the assertion is on the compacted artifact**, not on the binary: `.2`
must exist, hold the leases just driven in, and hold exactly one row per
address. It can only reach that state by `kea-lfc` running to completion. A
file with duplicate rows is a rotation that was never processed.

**The negative control is part of the gate, not a one-off.** It builds a
derived image with `kea-lfc` replaced by `exit 1`, confirms the assertion goes
red, and confirms the failure really is silent. It runs on every CI run, on
both architectures, so the gate cannot quietly stop discriminating.

**Cost.** The full smoke suite goes from ~40s to ~81s. Waiting is a poll, not
a fixed sleep, so a slow runner does not flake it.

**Rejected: asserting on `LFC_*` log tokens.** Easier to match and weaker
evidence — `DHCPSRV_MEMFILE_LFC_START` is emitted by `kea-dhcp4` *before* it
execs `kea-lfc`, so it is logged identically when the binary is broken. It
proves Kea tried, which is not the question.

**Rejected: a second checked-in config.** The variant is derived from
`test/kea-dhcp4-smoke.conf` with `sed`, so the two cannot drift.

---

## D23 — Put Kea in its own SBOM

**Decision.** The builder generates an SPDX document describing Kea and ships
it at `/usr/share/sbom/kea.spdx.json`. BuildKit's scanner merges it into the
published SBOM attestation.

**The defect.** `buildx --sbom=true` catalogues the *package manager's* view
of an image. Kea is compiled from source and copied out of a builder stage,
so it was invisible. Measured on the published `kea-dhcp4:3.2`:

- 25 apk packages catalogued — `busybox`, `tzdata`, `ssl_client`, all correct
- the string `kea` **absent from the SBOM entirely**
- while the image ships 23 `libkea-*.so` libraries, the daemon, and a dozen
  hook libraries

An SBOM that lists `tzdata` but not the DHCP server is worse than no SBOM,
because it reads as diligence. This was P5-1 in the review, filed as "verify
first" — verified, and real.

**One correction to the review.** It suggested injecting Kea *plus Boost and
log4cplus*. `ldd` on the daemon shows **no Boost at all** — Kea uses Boost
headers only, so there is no runtime component to declare — and `log4cplus`
and OpenSSL are apk packages already correctly catalogued. The gap was Kea
alone.

**How it lands.** Verified against a real build rather than assumed: with the
document present, the published attestation gains a `kea` package with its
version, MPL-2.0, the upstream URL, and a CPE. The CPE matters beyond
inventory — `cpe:2.3:a:isc:kea:3.2.0` is what lets a vulnerability scanner
match Kea CVEs at all, which is a precondition for the scanning work in P4-1.

Note the merge is lossy in one place: syft drops the SPDX `checksums` field,
so the source hash survives in the purl
(`pkg:generic/kea@3.2.0?checksum=sha256:…`) rather than in `checksums`. The
shipped document carries both.

**The checksum is the honest use of the hash D2 deleted.** That amendment
removed a bare `sha256sum` because it compared the hash to nothing. Recording
the same hash here, as the checksum of the source the binaries were built
from, is the use that was always worth having: a statement about provenance,
not a check pretending to be one.

**Reproducible output.** The document has no live timestamp — it derives from
`SOURCE_DATE_EPOCH`, defaulting to the epoch. A timestamp would change the
layer between the test build and the push build and trip D21's
tested-equals-published assertion.

**Asserted in two places, because they are two different claims:**

| Where | Claim |
|---|---|
| smoke gate 2 and gate 5 | every image *ships* a valid document agreeing with the image's version |
| `fuse`, after tagging | the *published attestation* names Kea at the right version |

The first proves the input exists; only the second proves buildx merged it.
A scanner silently skips a document it cannot parse, so without the second
the SBOM could quietly revert to omitting Kea with a green build.

**Two of my own mistakes here, both the recurring pattern.** The validator
started as an inline `python3 -c` whose nested quote escaping was wrong; the
caller discarded stderr, so it failed for a reason nobody could see. It is
now `test/check-sbom-doc.py`, a real file with its own tests. And the first
version of those tests passed a command string that never executed, so all
three cases reported "not valid JSON" — three green ticks, none testing what
they claimed. The tests now assert the *expected message*, not just a
non-zero exit.

