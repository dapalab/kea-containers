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

**Decision.** Publish a fifth image carrying `perfdhcp`, `kea-shell`,
`kea-admin` and `keactrl`.

**Why.** A load generator does not belong inside a production DHCP server
image, but CI needs `perfdhcp` to prove a real handshake. Splitting it out
solves that and closes a real gap in ISC's images: their issue #46 is
"database schema update impossible because kea-admin missing in docker images".

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

**Known deviation.** ISC's `kea-dhcp-ddns` image installs `isc-kea-gss-tsig`.
We disable `krb5`, so our DDNS image has no GSS-TSIG support. This is an
open-source hook, not a premium one, so it is a genuine functional gap rather
than a licensing one. Flagged for v2.

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

## D9 — Ship inert default configs

**Decision.** The default config baked into each image defines no subnets and
no DDNS domains. The daemon starts, logs and answers its control socket, but
serves nothing.

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

## D13 — Deferred: database backends

**Decision.** memfile only in v1.

**Cost of adding them later, measured from Alpine 3.24 `APKINDEX`:**

| | Build stage | Runtime stage |
|---|---|---|
| MySQL | `mariadb-connector-c-dev` 244 KiB | `mariadb-connector-c` 691 KiB |
| PostgreSQL | `libpq-dev` 1.87 MiB | `libpq` 386 KiB |

About **+1 MB on the runtime image** and two Meson flags. Kea discovers both
purely via pkg-config (`dependency('mariadb')`, `dependency('libpq')`), so
there is no `pg_config` hunting. This is close enough to free that the non-goal
is worth revisiting — recorded here so the decision is informed rather than
inherited.

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
| `kea-tools` | 122 MB | `python3` for `kea-shell` accounts for most of the difference |

Transfer size tells the more useful story:

| | |
|---|---|
| `kea-dhcp4` alone | 18.9 MB |
| All four together | **42.2 MB** |

Four layers are shared by all four images (Alpine base, runtime packages, the
`libkea-*` stack, licences). Pulling all four therefore costs roughly 2.2x one
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
