# Configuration

## Start from the template

Every image ships a commented config template at its normal path, for example
`/etc/kea/kea-dhcp4.conf`. It's designed to be safe out of the box: there are
no subnets defined, so the server starts, logs, and answers its control
socket, but never hands out an address until you tell it to.

It also doubles as a reference. It covers what most home and small-office
networks need:

- subnets, pools and host reservations by MAC address
- per-reservation options, and global reservations for devices that roam
- dynamic DNS wiring and TSIG keys
- prefix delegation and DUID reservations for IPv6
- a short guide to the open-source hook libraries worth knowing about:
  `lease_cmds`, `ha`, `flex_id`, `run_script`, `ping_check` and `subnet_cmds`

Copy it out of the image you've pulled, edit it, and check it:

```bash
docker run --rm ghcr.io/dapalab/kea-dhcp4:3.2 \
  cat /etc/kea/kea-dhcp4.conf > kea-dhcp4.conf

$EDITOR kea-dhcp4.conf

docker run --rm -v ./kea-dhcp4.conf:/tmp/c.conf:ro \
  ghcr.io/dapalab/kea-dhcp4:3.2 kea-dhcp4 -t /tmp/c.conf
```

Kea understands `//` and `/* */` comments, so you can uncomment blocks where
they sit. The same templates are in the repository under
[`build/config/`](../build/config/), and they're the exact files built into
the images. Complete worked examples are in [`examples/`](../examples/).

ISC's own images ship a config that starts handing out `192.168.50.0/24`
straight away. On a macvlan network, that could mean a second DHCP server
appearing on your LAN before you've configured anything, so these images
start inert instead.

## Talking to a running server

Each image includes `nc`, which is also what the healthcheck uses. So you can
send Kea control commands without installing anything extra:

```bash
docker exec kea4 sh -c \
  'echo "{\"command\":\"status-get\"}" | nc -U -N -w3 /run/kea/control_socket_4'
```

This works for all of Kea's control commands: `config-get`, `config-reload`,
`lease4-get`, `statistic-get-all`, `list-commands` and the rest. Pipe the
output through `jq` on the host if you'd like it formatted.

If you've turned on the HTTP control socket, `curl` works too:

```bash
curl -s -u admin:$PASS -H 'Content-Type: application/json' \
  -d '{"command":"status-get","service":["dhcp4"]}' http://127.0.0.1:8000/
```

There's an example with authentication set up in
[`examples/kea-dhcp4-http-control.conf`](../examples/kea-dhcp4-http-control.conf).

You don't need the control API for high availability or dynamic DNS. HA
runs inside the servers themselves (via `libdhcp_ha.so`), and the DHCP servers
send DNS updates to `kea-dhcp-ddns` directly over UDP port 53001. The control
API is just for you.

`kea-shell`, ISC's Python client for the API, isn't included: it would bring
in Python and roughly double the image size, and `nc` or `curl` do the same
job.

## Dynamic DNS with BIND9

`kea-dhcp-ddns` can update your DNS zones as leases come and go. It supports
**TSIG**, the shared-key method that BIND9, NSD, Knot and PowerDNS all use for
secure updates. It's built in, so you don't need a hook library.

Create a key:

```bash
tsig-keygen -a HMAC-SHA256 kea-ddns-key
```

Add it to `named.conf` and allow it to update your zone with
`allow-update { key "kea-ddns-key"; };`.

Kea 3.x wants the secret **in a file** rather than written into the config
(it rejects an inline secret with *"use of clear text TSIG 'secret' is NOT
SECURE"*). So save it to a file that only Kea can read:

```bash
printf '%s' 'YOUR_BASE64_SECRET' > ddns-key.secret
chmod 600 ddns-key.secret && sudo chown 10000:10000 ddns-key.secret
docker run -v ./ddns-key.secret:/etc/kea/ddns-key.secret:ro ...
```

A complete example, with both the Kea and the BIND9 side, is in
[`examples/kea-dhcp-ddns-bind9.conf`](../examples/kea-dhcp-ddns-bind9.conf).

**Using Active Directory DNS?** AD uses GSS-TSIG, a Kerberos-based variant,
which these images don't include (ISC's do). BIND9 and friends don't need it.
If you do, open an issue: it's about 2 MB and one build flag.

## Using a database for leases

By default, leases live in a CSV file under `/var/lib/kea`. You can use MySQL
or PostgreSQL instead. Set the database up with `kea-admin` from the
`kea-tools` image, and give Kea its password from a file rather than in the
config:

```json
"lease-database": {
  "type": "mysql",
  "host": "db.example.net",
  "name": "kea",
  "user": "kea",
  "password-file": "/run/secrets/kea-db-password"
}
```

If you ever move between Kea 3.0 and 3.2 with a database,
[Upgrading](upgrading.md) walks you through the schema migration.
