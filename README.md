# kea-containers

Container images for the [ISC Kea](https://www.isc.org/kea/) DHCP server, for
both **linux/amd64** and **linux/arm64** — so Kea runs just as well on a
Raspberry Pi as on an x86 server.

Each image is compiled from ISC's official source, which is checked against
ISC's signature before the build starts. Every published image is signed too,
so you can check it came from here.

> **A community project.** These images are built and maintained here, not by
> Internet Systems Consortium, and ISC doesn't endorse them. If something goes
> wrong with an image, please open an issue in
> [this repository](https://github.com/dapalab/kea-containers/issues) rather
> than with ISC. ISC publishes its own official images on
> [Cloudsmith](https://cloudsmith.io/~isc/repos/docker/packages/); at the time
> of writing those are amd64 only, which is why this project exists.

## Quick start

This gets a DHCPv4 server running on your LAN. It uses a *macvlan* network,
which gives the container its own address on your network so it can see DHCP
requests. [Networking](docs/networking.md) explains that choice and the
alternatives.

**1. Copy the config template out of the image.** It's heavily commented, and
safe as it is: until you add a subnet, Kea starts up but won't hand out any
addresses.

```bash
docker run --rm ghcr.io/dapalab/kea-dhcp4:3.2 \
  cat /etc/kea/kea-dhcp4.conf > kea-dhcp4.conf
```

**2. Edit it.** Uncomment the subnet block and fill in your network's details.

```bash
$EDITOR kea-dhcp4.conf
```

**3. Check it** before it goes anywhere near your network:

```bash
docker run --rm -v ./kea-dhcp4.conf:/tmp/c.conf:ro \
  ghcr.io/dapalab/kea-dhcp4:3.2 kea-dhcp4 -t /tmp/c.conf
```

**4. Run it.** Change `eth0`, the subnet and the addresses to match your
network:

```bash
docker network create -d macvlan \
  --subnet 192.168.50.0/24 --gateway 192.168.50.1 \
  -o parent=eth0 dhcp-net

docker run -d --name kea4 --restart unless-stopped \
  --network dhcp-net --ip 192.168.50.2 \
  -v ./kea-dhcp4.conf:/etc/kea/kea-dhcp4.conf:ro \
  -v kea-leases:/var/lib/kea \
  ghcr.io/dapalab/kea-dhcp4:3.2
```

**5. Check it's healthy:**

```bash
docker ps --filter name=kea4    # STATUS should say (healthy)
docker logs kea4                # look for DHCP4_STARTED
```

That's it. If something doesn't look right,
[Troubleshooting](docs/troubleshooting.md) covers the common snags. For
DHCPv4, DHCPv6 and dynamic DNS together, there's a Compose file in
[`examples/docker-compose.yml`](examples/docker-compose.yml).

## The images

| Image | What it is | Kea versions |
|---|---|---|
| `ghcr.io/dapalab/kea-dhcp4` | DHCPv4 server | 3.2, 3.0 |
| `ghcr.io/dapalab/kea-dhcp6` | DHCPv6 server | 3.2, 3.0 |
| `ghcr.io/dapalab/kea-dhcp-ddns` | Dynamic DNS updates | 3.2, 3.0 |
| `ghcr.io/dapalab/kea-ctrl-agent` | REST control agent | 3.0 only |
| `ghcr.io/dapalab/kea-tools` | `kea-admin`, `perfdhcp`, `keactrl`, `kea-lfc` | 3.2, 3.0 |

`kea-ctrl-agent` is only built for 3.0, because Kea 3.2 dropped it: the
servers now have their own HTTP control sockets. `kea-tools` isn't a server.
It's the admin toolbox, and you'll need it to set up or upgrade a
MySQL/PostgreSQL lease database.

## Which tag?

- **`3.2`** is the current stable branch.
- **`3.0-lts`** is ISC's long-term support branch. It gets fixes for longer
  than 3.2 does, so it's a good choice if you'd rather change versions less
  often.

Both tags are rebuilt every week to pick up Alpine security fixes. If you use
Renovate or Dependabot, pin the digest (`kea-dhcp4:3.2@sha256:…`) and let the
bot tell you when there's a new build. If you'd rather pin by hand, every
build also has a tag that never changes, like `3.2.0-20260920-2244`.
[Tags and updates](docs/tags-and-updates.md) has the full picture.

## Guides

| | |
|---|---|
| [Networking](docs/networking.md) | Getting DHCP traffic to a container: macvlan, host networking, relays |
| [Configuration](docs/configuration.md) | The config templates, the control API, dynamic DNS with BIND9 |
| [Tags and updates](docs/tags-and-updates.md) | What each tag means, how rebuilds work, Renovate setup |
| [Security](docs/security.md) | Non-root and capabilities, verifying signatures, how the build is checked |
| [Kubernetes](docs/kubernetes.md) | Running on a cluster, with a hardened example manifest |
| [Upgrading](docs/upgrading.md) | Moving between 3.0 and 3.2, including database migrations |
| [Differences from ISC's images](docs/differences-from-isc.md) | What's different here, and why |
| [Troubleshooting](docs/troubleshooting.md) | Common problems and how to fix them |
| [Behind the scenes](docs/DECISIONS.md) | Every design decision, the evidence for it, and what was tried first |

## Licence

This repository is [MPL-2.0](LICENSE), the same licence as Kea.

Kea is Copyright © 2009-2026 Internet Systems Consortium, Inc., licensed under
MPL-2.0. Its `COPYING` and `AUTHORS` files ship inside every image at
`/usr/share/licenses/kea/`. See [NOTICE](NOTICE) for full attribution.
