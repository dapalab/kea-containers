# syntax=docker/dockerfile:1
# SPDX-License-Identifier: MPL-2.0
#
# Unofficial community multi-arch container images for ISC Kea DHCP.
# Not affiliated with or endorsed by Internet Systems Consortium.
#
# Kea is built from the official ISC source tarball, whose detached PGP
# signature is verified against a vendored copy of ISC's code-signing
# keyblock. The keyring contains ONLY ISC's published keys, so a tarball
# signed by anything else fails with "No public key" and the build stops.
#
# One builder stage compiles Kea once per architecture; every runtime image
# is carved out of that single install tree and shares one base layer.

ARG ALPINE_REF=alpine:3.24

###############################################################################
# builder - compiles Kea once; its output feeds every image below
###############################################################################
FROM ${ALPINE_REF} AS builder

ARG KEA_VERSION
ARG BUILD_JOBS=3

RUN apk add --no-cache \
        build-base meson ninja-build pkgconf \
        boost-dev log4cplus-dev openssl-dev \
        bison flex python3 linux-headers \
        curl gnupg tar xz

WORKDIR /build
COPY keys/isc-keyblock.asc /build/isc-keyblock.asc

# Fetch and cryptographically verify the upstream tarball. Fails closed:
# a bad signature, a missing signature, or a signer outside ISC's keyblock
# all abort the build.
RUN set -eux; \
    test -n "${KEA_VERSION}"; \
    base="https://downloads.isc.org/isc/kea/${KEA_VERSION}"; \
    curl -fsSL -o kea.tar.xz     "${base}/kea-${KEA_VERSION}.tar.xz"; \
    curl -fsSL -o kea.tar.xz.asc "${base}/kea-${KEA_VERSION}.tar.xz.asc"; \
    export GNUPGHOME=/build/.gnupg; \
    mkdir -p "${GNUPGHOME}"; \
    chmod 700 "${GNUPGHOME}"; \
    gpg --batch --quiet --import isc-keyblock.asc; \
    echo "--- verifying kea-${KEA_VERSION}.tar.xz against ISC keyblock ---"; \
    gpg --batch --verify kea.tar.xz.asc kea.tar.xz; \
    sha256sum kea.tar.xz; \
    mkdir -p src; \
    tar -xf kea.tar.xz -C src --strip-components=1; \
    rm -rf "${GNUPGHOME}"

WORKDIR /build/src

# Every optional dependency is pinned explicitly. They default to 'auto' in
# meson.options, which would silently enable a backend if a -dev package
# happened to be present - making the image contents depend on the build
# environment rather than on this file.
RUN set -eux; \
    meson setup build \
        --prefix=/usr \
        --sysconfdir=/etc \
        --localstatedir=/var \
        --buildtype=release \
        -D runstatedir=/run \
        -D crypto=openssl \
        -D cpp_std=c++17 \
        -D mysql=disabled \
        -D postgresql=disabled \
        -D netconf=disabled \
        -D krb5=disabled \
        -D tests=disabled \
        -D fuzz=disabled; \
    meson compile -C build -j "${BUILD_JOBS}"; \
    DESTDIR=/stage meson install -C build --no-rebuild --quiet; \
    install -D -m 0644 COPYING /stage/usr/share/licenses/kea/COPYING; \
    install -D -m 0644 AUTHORS /stage/usr/share/licenses/kea/AUTHORS

###############################################################################
# runtime-base - one shared layer behind every published image
#
# Carries the runtime shared libraries, the full libkea-*.so stack and the
# open-source hook libraries. Because every image below starts FROM this exact
# stage, the registry stores these layers once and a user pulling all five
# downloads them once. No build tooling is present.
###############################################################################
FROM ${ALPINE_REF} AS runtime-base

# UID/GID are fixed and documented so bind-mounted lease directories can be
# chowned ahead of time. Changing these is a breaking change for users.
ARG KEA_UID=10000
ARG KEA_GID=10000

RUN set -eux; \
    apk add --no-cache libstdc++ log4cplus libcrypto3 libssl3 tzdata netcat-openbsd; \
    addgroup -S -g "${KEA_GID}" kea; \
    adduser -S -u "${KEA_UID}" -G kea -H -s /sbin/nologin kea; \
    mkdir -p /etc/kea /var/lib/kea /run/kea; \
    chown kea:kea /var/lib/kea /run/kea; \
    # Kea 3.x refuses to use a unix control socket whose directory is more
    # permissive than 0750, and refuses a lease directory it does not trust.
    # A default 0755 from mkdir is rejected at config-parse time.
    chmod 0750 /run/kea /var/lib/kea

# Shared Kea libraries + hook libraries, and the licence texts we are obliged
# to redistribute with the binaries (Kea is MPL-2.0).
COPY --from=builder /stage/usr/lib/ /usr/lib/
COPY --from=builder /stage/usr/share/licenses/kea/ /usr/share/licenses/kea/

###############################################################################
# kea-dhcp4
#
# kea-lfc is included because memfile leases require it: kea-dhcp4 execs it
# to compact the lease CSV. Without it, lease file cleanup silently fails.
#
# CAP_NET_RAW        raw/packet socket for the DHCPv4 broadcast path
# CAP_NET_BIND_SERVICE   bind UDP/67
#
# File capabilities are set on the binary so the daemon can run as a non-root
# user and still acquire them. Docker's default bounding set includes both; if
# you run with --cap-drop=ALL you must add them back explicitly.
###############################################################################
FROM runtime-base AS dhcp4
COPY --from=builder /stage/usr/sbin/kea-dhcp4 /usr/sbin/kea-dhcp4
COPY --from=builder /stage/usr/sbin/kea-lfc   /usr/sbin/kea-lfc
COPY config/kea-dhcp4.conf /etc/kea/kea-dhcp4.conf
RUN set -eux; \
    apk add --no-cache --virtual .setcap libcap; \
    setcap cap_net_raw,cap_net_bind_service=+ep /usr/sbin/kea-dhcp4; \
    apk del .setcap; \
    getcap /usr/sbin/kea-dhcp4 || true
# Ask the daemon for its own status over the unix control socket. This proves
# the daemon is actually answering, not merely that the process exists or the
# socket file was created.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "echo '{\"command\":\"status-get\"}' | nc -U -N -w3 /run/kea/control_socket_4 | grep -q '\"result\": *0'"]
USER 10000:10000
EXPOSE 67/udp 67/tcp 8000/tcp 8001/tcp
VOLUME ["/etc/kea", "/var/lib/kea"]
CMD ["/usr/sbin/kea-dhcp4", "-c", "/etc/kea/kea-dhcp4.conf"]

###############################################################################
# kea-dhcp6 - CAP_NET_BIND_SERVICE only; DHCPv6 is UDP multicast, not raw
###############################################################################
FROM runtime-base AS dhcp6
COPY --from=builder /stage/usr/sbin/kea-dhcp6 /usr/sbin/kea-dhcp6
COPY --from=builder /stage/usr/sbin/kea-lfc   /usr/sbin/kea-lfc
COPY config/kea-dhcp6.conf /etc/kea/kea-dhcp6.conf
RUN set -eux; \
    apk add --no-cache --virtual .setcap libcap; \
    setcap cap_net_bind_service=+ep /usr/sbin/kea-dhcp6; \
    apk del .setcap
# Ask the daemon for its own status over the unix control socket. This proves
# the daemon is actually answering, not merely that the process exists or the
# socket file was created.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "echo '{\"command\":\"status-get\"}' | nc -U -N -w3 /run/kea/control_socket_6 | grep -q '\"result\": *0'"]
USER 10000:10000
EXPOSE 547/udp 547/tcp 8000/tcp 8001/tcp
VOLUME ["/etc/kea", "/var/lib/kea"]
CMD ["/usr/sbin/kea-dhcp6", "-c", "/etc/kea/kea-dhcp6.conf"]

###############################################################################
# kea-dhcp-ddns - needs no capabilities at all; ports 53001 and 8000 are >1024
###############################################################################
FROM runtime-base AS dhcp-ddns
COPY --from=builder /stage/usr/sbin/kea-dhcp-ddns /usr/sbin/kea-dhcp-ddns
COPY config/kea-dhcp-ddns.conf /etc/kea/kea-dhcp-ddns.conf
# Ask the daemon for its own status over the unix control socket. This proves
# the daemon is actually answering, not merely that the process exists or the
# socket file was created.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "echo '{\"command\":\"status-get\"}' | nc -U -N -w3 /run/kea/control_socket_d2 | grep -q '\"result\": *0'"]
USER 10000:10000
EXPOSE 53001/udp 8000/tcp
VOLUME ["/etc/kea"]
CMD ["/usr/sbin/kea-dhcp-ddns", "-c", "/etc/kea/kea-dhcp-ddns.conf"]

###############################################################################
# kea-ctrl-agent  (Kea 3.0 ONLY)
#
# Removed upstream in 3.2, once the daemons gained native HTTP/TLS control
# sockets. versions.json carries a per-branch image list precisely so this
# stage is only ever built for 3.0; building it against 3.2 sources will fail
# at the COPY below, which is the correct and loud outcome.
#
# Needs no capabilities: port 8000 is above 1024.
#
# It must share /run/kea with the daemons it proxies, so /run/kea is left as a
# declared volume rather than a private directory.
###############################################################################
FROM runtime-base AS ctrl-agent
COPY --from=builder /stage/usr/sbin/kea-ctrl-agent /usr/sbin/kea-ctrl-agent
COPY config/kea-ctrl-agent.conf /etc/kea/kea-ctrl-agent.conf

# The agent speaks HTTP, not a unix socket, so the probe has to be a real HTTP
# request - piping bare JSON at it does not work.
#
# Success is any HTTP status line. That is deliberate: a 401 from an agent with
# authentication enabled still proves the server is alive and answering, and
# the healthcheck has no business holding credentials.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "b='{\"command\":\"version-get\"}'; printf 'POST / HTTP/1.0\\r\\nContent-Type: application/json\\r\\nContent-Length: %d\\r\\n\\r\\n%s' ${#b} \"$b\" | nc -w3 127.0.0.1 8000 | grep -q '^HTTP/'"]
USER 10000:10000
EXPOSE 8000/tcp
VOLUME ["/etc/kea", "/run/kea"]
CMD ["/usr/sbin/kea-ctrl-agent", "-c", "/etc/kea/kea-ctrl-agent.conf"]

###############################################################################
# kea-tools - perfdhcp, kea-shell, kea-admin, keactrl
#
# Not a daemon. Exists so users have the administrative tooling ISC's images
# omit (their issue #46: schema updates impossible, kea-admin missing), and so
# CI has a first-class way to drive the perfdhcp functional test.
#
# kea-shell is deliberately NOT included. It is a standalone Python client for
# the control API and nothing depends on it: HA runs inside the daemons via
# libdhcp_ha.so with peers talking HTTP directly, and DDNS is the daemons
# sending NCRs to kea-dhcp-ddns over UDP. Shipping it would drag python3 into
# the image and roughly double its size. The control API is reachable with the
# netcat already present in every image - see README.
#
# perfdhcp gets CAP_NET_RAW so it can craft DHCPv4 packets as a non-root user.
###############################################################################
FROM runtime-base AS tools
COPY --from=builder /stage/usr/sbin/perfdhcp   /usr/sbin/perfdhcp
COPY --from=builder /stage/usr/sbin/kea-lfc    /usr/sbin/kea-lfc
COPY --from=builder /stage/usr/sbin/kea-admin  /usr/sbin/kea-admin
COPY --from=builder /stage/usr/sbin/keactrl    /usr/sbin/keactrl
RUN set -eux; \
    apk add --no-cache --virtual .setcap libcap; \
    setcap cap_net_raw,cap_net_bind_service=+ep /usr/sbin/perfdhcp; \
    apk del .setcap
USER 10000:10000
CMD ["/usr/sbin/perfdhcp", "-h"]
