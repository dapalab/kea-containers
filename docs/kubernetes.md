# Kubernetes

There's a complete, hardened manifest in
[`examples/kubernetes/kea-dhcp4.yaml`](../examples/kubernetes/kea-dhcp4.yaml).
It's a good starting point even if you end up writing your own, and its
comments explain each setting.

In short, it runs a single `kea-dhcp4` pod:

- with **`hostNetwork: true`**, so the server can hear DHCP broadcasts on the
  node's network (pod networking doesn't carry them)
- **pinned to one node** with a `nodeSelector`, since it binds UDP port 67 on
  that node
- as the **non-root** user 10000, with `runAsNonRoot` enforced
- with `fsGroup: 10000`, so it can write to its lease volume
- with a **read-only root filesystem**
- with **all capabilities dropped** except the two the server needs
- using the `Recreate` update strategy, so an old pod lets go of port 67 and
  its volume before the new one starts

Before applying it, set the node name, your interface and subnet in the
ConfigMap, and (if needed) your storage class.

## Things worth knowing if you write your own

**Kubernetes ignores the image's `VOLUME` and `HEALTHCHECK`.** Docker uses
both; Kubernetes uses neither. Give every writable path its own volume
(`/var/lib/kea` for leases, `/run/kea` for the control socket), and restate
the healthcheck as a probe. The example includes a probe that sends the same
`status-get` command the image's own healthcheck uses.

**Drop all capabilities, then add two back:**

```yaml
capabilities:
  drop: [ "ALL" ]
  add: [ "NET_BIND_SERVICE", "NET_RAW" ]
```

Dropping them all without adding these back stops the server starting, with
`exec /usr/sbin/kea-dhcp4: operation not permitted`.

**`allowPrivilegeEscalation: false` is fine to use.** These images get their
permissions from file capabilities, and you might expect this setting to
block them. It doesn't: the server keeps the permissions it needs and binds
port 67 normally.

**Run one replica, not a DaemonSet.** Several servers on the same network,
each with its own lease file, would hand out the same addresses twice. For
real high availability, use Kea's HA hook with a shared database. That's
beyond this example, but the [configuration template](configuration.md) points
you in the right direction.

**Pin a tag that doesn't move** if you're not using a tool to manage updates.
The example uses a stamped tag. See [Tags and updates](tags-and-updates.md)
for the options.

## Other ways to reach the network

`hostNetwork` is the most straightforward option on bare metal. If your
cluster already runs Multus with macvlan, that works too, and so does a DHCP
relay pointing at a Service.
