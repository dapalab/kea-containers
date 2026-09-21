# Troubleshooting

The problems people most often hit, and how to fix them. If yours isn't here,
please [open an issue](https://github.com/dapalab/kea-containers/issues). Your
Kea log (`docker logs <container>`) and the tag you're running will help a
lot.

## Kea is running but not handing out addresses

- **Have you added a subnet?** The built-in config deliberately has none, so a
  fresh container serves nothing until you configure it. See
  [Configuration](configuration.md).
- **Can it hear the requests?** On Docker's default bridge network, DHCP
  broadcasts never reach the container. Use macvlan, host networking, or a
  relay: see [Networking](networking.md).
- **Is it listening on the right interface?** `interfaces-config` needs to name
  the interface the requests arrive on: the container's own interface on
  macvlan, or the host's real one (like `eth0`) with host networking.
- **Is another DHCP server answering first?** Most routers have one built in.
  Turn it off for the subnet Kea is serving.

## The container shows as "unhealthy"

The healthcheck asks Kea for its status over its control socket, at
`/run/kea/control_socket_4` (or `_6` for DHCPv6, `_d2` for DDNS). If your own
config leaves out `control-sockets`, or puts the socket somewhere else, the
healthcheck can't reach it. Keep the socket from the template and the
healthcheck will work.

If the config is fine, `docker logs` usually says what went wrong.

## Kea can't write its lease file

The server runs as user 10000. A folder you bind-mount from the host belongs
to you or root, so Kea isn't allowed to write to it. Either give it to UID
10000:

```bash
sudo chown 10000:10000 ./leases
```

or use a named volume (`-v kea-leases:/var/lib/kea`), which Docker sets up
with the right owner automatically.

If you mount your own `/run/kea`, it also needs to belong to 10000 **and** be
mode `0750`. Kea 3.x won't use a socket directory that other users can read.

## `exec: operation not permitted`

The container can't use the capabilities the server needs. This usually means
`--cap-drop=ALL` (or `drop: [ALL]` on Kubernetes) without adding them back.
For `kea-dhcp4`, add back `NET_RAW` and `NET_BIND_SERVICE`; for `kea-dhcp6`,
just `NET_BIND_SERVICE`. [Security](security.md) has the full table.

## `docker ps` shows no ports

That's normal on macvlan. The container has its own IP address, so there's
nothing to map. Kea is listening on the container's own IP. Using `-p` on a
macvlan network does nothing.

## The host can't reach the container

Also normal on macvlan: Linux doesn't let a host talk to its own macvlan
containers through the same interface. Other machines on the LAN can reach
it fine. If the host needs to reach it too, use `ipvlan` (L2 mode) or add a
macvlan shim interface on the host.

## Kea won't start after switching between 3.0 and 3.2

If you store leases in MySQL or PostgreSQL, you'll see something like:

```
MySQL schema version mismatch: expected version: 35.0, found version: 30.0
```

Each Kea branch expects a particular database schema. Nothing has been
damaged: Kea stops before touching anything. Run the schema upgrade with
`kea-tools`, as described in [Upgrading](upgrading.md). Going back from 3.2
to 3.0 means restoring a backup, because schemas can't be downgraded.

## "The Kea server has not been compiled with" MySQL or PostgreSQL

It has been, and this message is misleading. In Kea 3.x each database backend
is a hook library, and this is what Kea says when the hook isn't loaded. Add
it to your config alongside the `lease-database` settings:

```json
"hooks-libraries": [
  { "library": "/usr/lib/kea/hooks/libdhcp_mysql.so" }
],
```

For PostgreSQL, use `libdhcp_pgsql.so`. There's a full example in
[Configuration](configuration.md#using-a-database-for-leases).

## `use of clear text TSIG 'secret' is NOT SECURE`

Kea 3.x wants DDNS keys in a file rather than written into the config. Use
`secret-file` instead of `secret`. The steps are in
[Configuration](configuration.md#dynamic-dns-with-bind9).

## An image pulled by digest isn't in `docker image ls`

If you pull with a digest, like
`ghcr.io/dapalab/kea-dhcp4:3.2.0@sha256:80d68…`, Docker may leave the image
out of `docker image ls`, or list it with the tag `<none>`. The image is
there; it just has no tag.

A digest identifies the image's exact contents. When a digest is given,
Docker fetches by digest alone and ignores the tag, even if you typed one, so
there's no tag to record. (It couldn't pick one anyway: several tags usually
point at the same digest, such as `3.2.0`, `3.2` and that build's stamped
tag.) That's also what makes a digest pin reliable: it keeps pointing at the
same image after `3.2.0` moves on to a newer build.

To see it:

```bash
docker image ls -a --digests ghcr.io/dapalab/kea-dhcp4
```

If you'd like it listed by name, give it a tag of your own. The tag only
exists on your machine and doesn't change what the pin points at:

```bash
docker tag ghcr.io/dapalab/kea-dhcp4@sha256:<digest> kea-dhcp4:pinned
```

Keep the tag in your pins even though Docker ignores it (`3.2@sha256:…`
rather than just `@sha256:…`). It tells people what the digest is, and
Renovate uses it to know which tag to follow.

## Renovate never suggests an update

You're probably pinned to a stamped tag like `3.2.0-20260920-2244`.
Renovate's standard settings can't follow those. Switch to a moving tag with
a pinned digest (`3.2@sha256:…`); [Tags and updates](tags-and-updates.md)
explains why and how.

## My backup file is empty

If you ran `mysqldump` or `pg_dump` through the `kea-tools` image, they aren't
in it, and the shell doesn't always make that obvious. Use your database's own
image to take the backup instead. [Upgrading](upgrading.md#take-a-backup-with-your-databases-own-image)
has the commands.
