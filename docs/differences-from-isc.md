# Differences from ISC's images

These images follow ISC's official ones wherever they can: the same config
paths, volume layout, ports, and one process per container. If you're coming
from ISC's images, your config should work as it is.

The main difference is how Kea gets into the image. ISC installs it from their
own x86_64-only packages; these images compile it from ISC's signed source
code, which is what makes arm64 possible.

The rest are deliberate choices, listed here so nothing catches you out:

| | ISC's images | These images | Why |
|---|---|---|---|
| **User** | Run as root | Run as user 10000, with only the capabilities they need | Least privilege. See [Security](security.md) |
| **Default config** | Starts serving `192.168.50.0/24` straight away | Starts with no subnets, so it hands out nothing until you configure it | A new container shouldn't start answering DHCP on your LAN before you've set it up |
| **API credentials** | Ship a password file containing `api-user-name:api-user-password` | Ship no credentials | Publicly known passwords shouldn't come built in |
| **HTTP control socket** | On `0.0.0.0:8000` by default | Off by default; the Unix socket is always there | Turn it on when you want it, with your own credentials |
| **Base image** | `alpine:3.24` | `alpine:3.24`, pinned by digest | The same base every time, until it's deliberately updated |
| **Healthcheck** | None | Checks the server answers `status-get` | So Docker and orchestrators can tell when it's healthy |
| **Architectures** | amd64 | amd64 and arm64 | The reason this project exists. ISC's arm64 request, [kea-docker#47](https://gitlab.isc.org/isc-projects/kea-docker/-/issues/47), has been open since January 2026 |
| **GSS-TSIG in DDNS** | Included | Not included | It's for Active Directory DNS; BIND9 and friends use plain TSIG, which is supported. See [Configuration](configuration.md#dynamic-dns-with-bind9) |
| **`kea-admin`** | Not in any image | In the `kea-tools` image | Needed for setting up and upgrading databases ([kea-docker#46](https://gitlab.isc.org/isc-projects/kea-docker/-/issues/46)) |

The reasoning behind each of these is in [Behind the scenes](DECISIONS.md).
