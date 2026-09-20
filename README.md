# kea-containers

Multi-architecture container images for the [ISC Kea](https://www.isc.org/kea/)
DHCP server, built from verified upstream source for **linux/amd64** and
**linux/arm64**.

> ### Unofficial
>
> **These images are not published by, affiliated with, or endorsed by Internet
> Systems Consortium.** They are community builds. ISC publishes its own
> official Kea images — see
> [cloudsmith.io/~isc/repos/docker](https://cloudsmith.io/~isc/repos/docker/packages/) —
> which are amd64-only at the time of writing.
>
> **Do not raise issues about these images with ISC.** Use this repository's
> issue tracker.

## Why this exists

ISC builds official Kea images for amd64 only. Their arm64 request
([kea-docker#47](https://gitlab.isc.org/isc-projects/kea-docker/-/issues/47))
has been open and untouched since January 2026, and an earlier multi-arch
attempt (#43) was closed without merging.

These images follow ISC's runtime conventions — config paths, volume layout,
exposed ports, single-process `CMD` — closely enough to be drop-in
replacements. Only the package-install step differs: Kea is compiled from the
official source tarball rather than installed from ISC's x86_64-only apk
repository, which is the whole reason this project exists.

Every deliberate deviation is listed in [Deviations](#deviations-from-iscs-images).

## Images

| Image | Purpose | Branches |
|---|---|---|
| `ghcr.io/dapalab/kea-dhcp4` | DHCPv4 server | 3.2, 3.0 |
| `ghcr.io/dapalab/kea-dhcp6` | DHCPv6 server | 3.2, 3.0 |
| `ghcr.io/dapalab/kea-dhcp-ddns` | Dynamic DNS updates | 3.2, 3.0 |
| `ghcr.io/dapalab/kea-ctrl-agent` | REST control agent | **3.0 only** |
| `ghcr.io/dapalab/kea-tools` | `perfdhcp`, `kea-admin`, `keactrl`, `kea-lfc` | 3.2, 3.0 |

`kea-ctrl-agent` exists only for 3.0. It was removed upstream in 3.2 once the
daemons gained native HTTP/TLS control sockets.

`kea-tools` is not a daemon. It carries the administrative tooling ISC's images
omit — notably `kea-admin`, whose absence makes database schema updates
impossible ([kea-docker#46](https://gitlab.isc.org/isc-projects/kea-docker/-/issues/46)).

It deliberately does **not** include `kea-shell`. That is a standalone Python
client for the control API, and including it would pull `python3` in and double
the image size. Nothing depends on it — see
[Talking to the control API](#talking-to-the-control-api).

## Tags

| Tag | Moves? | Meaning |
|---|---|---|
| `3.2.0` | no | Exact Kea version |
| `3.2` | yes | Latest patch on the 3.2 stable branch |
| `3.0` | yes | Latest patch on the 3.0 branch |
| `3.0-lts` | yes | Same as `3.0`; ISC's long-term support branch |

**There is no `latest` tag, and no bare `3` tag.** Both would silently move a
DHCP server across major or minor versions. A bare `3` is worse than useless
here: it would resolve to 3.2, but 3.0 is the LTS and outlives 3.2 by eleven
months, so pinning `3` for stability would give you the *shorter*-lived branch.

For anything you actually care about, **pin the digest**:

```bash
docker pull ghcr.io/dapalab/kea-dhcp4@sha256:<digest>
```

## Running DHCP in a container — read this first

A DHCP server needs to see broadcast traffic on the segment it serves. On
Docker's default bridge network it will not. You have three realistic options:

### macvlan / ipvlan (recommended)

Gives the container its own MAC/IP directly on the physical segment.

```bash
docker network create -d macvlan \
  --subnet 192.168.50.0/24 --gateway 192.168.50.1 \
  -o parent=eth0 dhcp-net

docker run -d --name kea4 \
  --network dhcp-net --ip 192.168.50.2 \
  -v ./kea-dhcp4.conf:/etc/kea/kea-dhcp4.conf:ro \
  -v kea-leases:/var/lib/kea \
  ghcr.io/dapalab/kea-dhcp4:3.2
```

Caveat: by design the host cannot reach a macvlan container over that
interface. Use `ipvlan` in L2 mode, or add a host-side macvlan shim, if you
need host-to-container access.

### host networking

Simplest, and the container sees all broadcast traffic:

```bash
docker run -d --name kea4 --network host \
  -v ./kea-dhcp4.conf:/etc/kea/kea-dhcp4.conf:ro \
  ghcr.io/dapalab/kea-dhcp4:3.2
```

You lose network isolation, and `interfaces-config` must name the real host
interface.

### bridge + relay

Keep the container on a bridge and point a DHCP relay at it. Works, adds a
moving part, and `interfaces-config` must be set up for relayed traffic.

## Capabilities

These images run as a **non-root** user (`kea`, UID/GID **10000**). Required
capabilities are set as file capabilities on the binaries, so they work under
Docker's default capability set with **no `--cap-add` needed**:

| Image | Capabilities | Why |
|---|---|---|
| `kea-dhcp4` | `CAP_NET_RAW`, `CAP_NET_BIND_SERVICE` | Raw socket for the DHCPv4 broadcast path; bind UDP/67 |
| `kea-dhcp6` | `CAP_NET_BIND_SERVICE` | Bind UDP/547 |
| `kea-dhcp-ddns` | *none* | Ports 53001 and 8000 are above 1024 |
| `kea-ctrl-agent` | *none* | Port 8000 is above 1024 |
| `kea-tools` | `CAP_NET_RAW` | `perfdhcp` crafts raw DHCPv4 packets |

**Do not run these privileged.** If you harden with `--cap-drop=ALL`, you must
add the capabilities back:

```bash
docker run --cap-drop=ALL \
  --cap-add=NET_RAW --cap-add=NET_BIND_SERVICE \
  ghcr.io/dapalab/kea-dhcp4:3.2
```

> Subtlety worth knowing: in Docker, `--cap-add` alone does **not** give a
> capability to a non-root process — it only widens the container's bounding
> set. The file capabilities baked into these binaries are what actually let
> the unprivileged `kea` user acquire them.

## Volumes and paths

| Path | Contents | Images |
|---|---|---|
| `/etc/kea` | Configuration | all |
| `/var/lib/kea` | Lease database (memfile CSV) | dhcp4, dhcp6 |
| `/run/kea` | Unix control sockets | all |
| `/usr/share/licenses/kea` | Kea `COPYING` and `AUTHORS` | all |

Because the daemons run as UID 10000, a bind-mounted lease directory must be
writable by that UID:

```bash
mkdir -p ./leases && sudo chown 10000:10000 ./leases
docker run -v ./leases:/var/lib/kea ...
```

Named volumes do not have this problem — Docker sets ownership from the image.

## Configuration

Each image ships a **commented configuration template** at its normal config
path. It is safe as-is — no subnets are defined, so the daemon starts, logs and
answers its control socket but will never offer a lease — and it doubles as the
reference for building a real config.

Start from the copy inside the image you already pulled:

```bash
docker run --rm ghcr.io/dapalab/kea-dhcp4:3.2 \
  cat /etc/kea/kea-dhcp4.conf > kea-dhcp4.conf

$EDITOR kea-dhcp4.conf     # uncomment and fill in the subnet block

docker run --rm -v ./kea-dhcp4.conf:/tmp/c.conf:ro \
  ghcr.io/dapalab/kea-dhcp4:3.2 kea-dhcp4 -t /tmp/c.conf   # validate first

docker run -d --name kea4 --network dhcp-net \
  -v ./kea-dhcp4.conf:/etc/kea/kea-dhcp4.conf:ro \
  -v kea-leases:/var/lib/kea \
  ghcr.io/dapalab/kea-dhcp4:3.2
```

Kea accepts `//` and `/* */` comments, so you can uncomment blocks in place.
The same files are browsable in the repository at
[`build/config/`](build/config/) — they are not copies, they are the exact
files baked into the images.

The templates cover what a homelab actually needs: subnets and pools, host
reservations by MAC, per-reservation options, global reservations for roaming
clients, DDNS wiring, prefix delegation and DUID reservations for v6, TSIG keys
for DDNS, and a guide to the open-source hook libraries worth knowing about
(`lease_cmds`, `ha`, `flex_id`, `run_script`, `ping_check`, `subnet_cmds`).

This differs from ISC's images, which ship a live `192.168.50.0/24` pool bound
to `eth0`. A DHCP server that starts up already willing to hand out addresses
is a poor default for an image intended to run on macvlan.

Fully worked examples are in [`examples/`](examples/).

## Talking to the control API

Every image ships `nc` (used by the healthcheck), so the full control API is
reachable without `kea-shell` or any extra tooling:

```bash
docker exec kea4 sh -c \
  'echo "{\"command\":\"status-get\"}" | nc -U -N -w3 /run/kea/control_socket_4'
```

All 33 commands work this way — `config-get`, `config-reload`, `lease4-get`,
`statistic-get-all`, `list-commands` and the rest. Pipe through `python3 -m
json.tool` or `jq` on the host if you want it formatted.

If you have enabled the HTTP control socket, `curl` works equally well:

```bash
curl -s -u admin:$PASS -H 'Content-Type: application/json' \
  -d '{"command":"status-get","service":["dhcp4"]}' http://127.0.0.1:8000/
```

> **Note on HA and DDNS.** Neither depends on any of this. High availability
> runs inside the daemons via `libdhcp_ha.so`, with peers talking HTTP directly
> to each other; DDNS is the DHCP daemons sending name-change requests to
> `kea-dhcp-ddns` over UDP/53001. The control API is for administration only.

## Deviations from ISC's images

Everything here is a deliberate choice, recorded with reasoning in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

| # | ISC | Here | Why |
|---|---|---|---|
| 1 | Runs as root | Non-root UID 10000 + file capabilities | Least privilege |
| 2 | Ships a live `192.168.50.0/24` pool | Inert config, no subnets | Safe default on macvlan |
| 3 | Ships `hiddens` = `api-user-name:api-user-password` | No credentials shipped | Working credentials in a public image are indefensible |
| 4 | HTTP control socket on `0.0.0.0:8000` by default | Unix socket only by default | Opt in, with your own auth |
| 5 | Base image `alpine:3.24`, unpinned | Pinned by digest | Reproducibility |
| 6 | No healthcheck | `HEALTHCHECK` via control socket | Orchestrator integration |
| 7 | Installs from ISC's apk repo (x86_64) | Compiles from verified source | Enables arm64 |
| 8 | DDNS image includes GSS-TSIG | Not included in v1 | Kerberos dependency; tracked for v2 |
| 9 | No `kea-admin` in any image | `kea-tools` image | Closes kea-docker#46 |

## Supply chain

Kea is built from the official ISC source tarball. Its detached PGP signature
is verified at build time against a **vendored** copy of ISC's code-signing
keyblock, committed at [`build/keys/isc-keyblock.asc`](build/keys/isc-keyblock.asc).

The build keyring contains ISC's published keys and nothing else, so a tarball
signed by any other key fails with `No public key` and the build aborts. The
keyblock is vendored rather than fetched so that an attacker controlling the
download path cannot supply both a tarball and a key that matches it.

ISC publishes **no checksum files** for Kea — only detached `.asc` signatures —
so there is no upstream SHA-256 to compare against. See `docs/DECISIONS.md` D2.

## Licence

This repository is [MPL-2.0](LICENSE), matching Kea.

Kea is Copyright © 2009-2026 Internet Systems Consortium, Inc. and is licensed
under MPL-2.0. Its `COPYING` and `AUTHORS` ship inside every image at
`/usr/share/licenses/kea/`. See [NOTICE](NOTICE) for full attribution.
