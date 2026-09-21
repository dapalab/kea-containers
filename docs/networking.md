# Networking

A DHCP server has to hear the broadcasts clients send when they first join
the network. On Docker's default bridge network it can't, because those
broadcasts never reach the container. So a DHCP container needs a little more
networking thought than most. There are three good ways to do it.

## macvlan (recommended)

macvlan gives the container its own MAC and IP address directly on your
physical network. As far as the rest of the LAN can tell, it's just another
machine.

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

Set `parent` to the host interface that's plugged into the network you want
to serve, and pick an `--ip` outside your DHCP pool.

Two things tend to surprise people the first time:

- **The host itself can't reach the container** over that interface. That's
  how macvlan works in Linux, not a fault in the image. If you need
  host-to-container traffic, use `ipvlan` in L2 mode instead, or add a
  macvlan "shim" interface on the host.
- **`docker ps` shows nothing under PORTS.** That's expected too. The
  container has its own address, so there's nothing to map, and `-p` has no
  effect on macvlan. Kea is listening on the container's own IP.

## Host networking

The simplest option. The container shares the host's network, so it sees
everything the host sees:

```bash
docker run -d --name kea4 --network host \
  -v ./kea-dhcp4.conf:/etc/kea/kea-dhcp4.conf:ro \
  -v kea-leases:/var/lib/kea \
  ghcr.io/dapalab/kea-dhcp4:3.2
```

The trade-off is that you give up network isolation. Also, `interfaces-config`
in your Kea config needs to name the host's real interface (`eth0`, `enp3s0`
and so on).

## Bridge network plus a DHCP relay

If the container has to stay on a normal bridge network, a DHCP relay (often
built into your router or switch) can forward requests to it. This works well,
but it's one more moving part, and Kea's `interfaces-config` needs setting up
for relayed traffic.

## Which one needs which

Only the two DHCP servers need any of this. `kea-dhcp-ddns` talks to your DNS
server over ordinary UDP, so a plain bridge network is fine for it, as the
[Compose example](../examples/docker-compose.yml) shows.

## Where things live

| Path | What's there | Which images |
|---|---|---|
| `/etc/kea` | Configuration | all |
| `/var/lib/kea` | Lease database (a CSV file, by default) | dhcp4, dhcp6 |
| `/run/kea` | Unix control sockets | all |
| `/usr/share/licenses/kea` | Kea's `COPYING` and `AUTHORS` | all |

The servers run as user `kea`, UID and GID **10000**. A **named volume**
(like `kea-leases` above) gets the right ownership automatically, so it's the
easiest choice.

If you'd rather bind-mount a folder from the host, give it to UID 10000 first,
or Kea won't be able to write its leases:

```bash
mkdir -p ./leases && sudo chown 10000:10000 ./leases
docker run -v ./leases:/var/lib/kea ...
```

The images declare their ports (67 for DHCPv4, 547 for DHCPv6, 53001 for
DDNS, 8000 for HTTP control), but that's only a label. Kea listens wherever
your config tells it to.
