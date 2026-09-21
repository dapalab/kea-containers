# syntax=docker/dockerfile:1
# SPDX-License-Identifier: MPL-2.0
#
# Unofficial community multi-arch container images for ISC Kea DHCP.
# Not affiliated with or endorsed by Internet Systems Consortium.
#
# Kea is built from the official ISC source tarball, whose detached PGP
# signature is checked against a copy of ISC's code-signing keys kept in this
# repository. The keyring holds only ISC's published keys, so a tarball signed
# by any other key fails with "No public key" and the build stops. The check
# looks for gpg's GOODSIG status token rather than its exit code, which is 0
# even when the signing key has expired or been revoked.
#
# One builder stage compiles Kea once per architecture. Every runtime image is
# made from that one install tree, and they all share one base layer.

ARG ALPINE_REF=alpine:3.24
ARG KEA_VERSION
ARG SOURCE_URL=https://github.com/dapalab/kea-containers
ARG REVISION
ARG CREATED

###############################################################################
# builder - compiles Kea once, for every image below
###############################################################################
FROM ${ALPINE_REF} AS builder

ARG KEA_VERSION
ARG BUILD_JOBS=3

RUN apk add --no-cache \
        build-base meson ninja-build pkgconf \
        boost-dev log4cplus-dev openssl-dev \
        mariadb-connector-c-dev libpq-dev \
        bison flex python3 linux-headers \
        curl gnupg tar xz

WORKDIR /build
COPY keys/isc-keyblock.asc /build/isc-keyblock.asc
COPY verify-tarball.sh     /usr/local/bin/verify-tarball.sh
COPY make-sbom.sh          /usr/local/bin/make-sbom.sh

# Download the upstream tarball and check its signature. Anything wrong stops
# the build: a bad or missing signature, a signer outside ISC's keyblock, or a
# signing key that has expired or been revoked.
#
# The check itself lives in verify-tarball.sh rather than inline here, so that
# test/verify-signature-test.sh can run the real script against expired,
# revoked, untrusted and tampered fixtures. A check written inline can't be
# tested. That script's header explains why `gpg --verify` alone isn't enough.
#
# There's deliberately no separate SHA-256 check. ISC doesn't publish checksum
# files (checked again 2026-09-20: .sha256, .sha512 and .SHA256 all return 404
# for 3.2.0 and 3.0.4), so any hash would be one we'd written down ourselves.
# A valid signature over these exact bytes is stronger anyway. This step used
# to end with a bare `sha256sum kea.tar.xz`, which printed a hash but compared
# it with nothing, so we removed it. See docs/DECISIONS.md D2.
RUN set -eux; \
    test -n "${KEA_VERSION}"; \
    base="https://downloads.isc.org/isc/kea/${KEA_VERSION}"; \
    curl -fsSL -o kea.tar.xz     "${base}/kea-${KEA_VERSION}.tar.xz"; \
    curl -fsSL -o kea.tar.xz.asc "${base}/kea-${KEA_VERSION}.tar.xz.asc"; \
    verify-tarball.sh kea.tar.xz kea.tar.xz.asc isc-keyblock.asc; \
    make-sbom.sh "${KEA_VERSION}" kea.tar.xz \
        "${base}/kea-${KEA_VERSION}.tar.xz" \
        /stage/usr/share/sbom/kea.spdx.json; \
    mkdir -p src; \
    tar -xf kea.tar.xz -C src --strip-components=1

WORKDIR /build/src

# Every optional feature is switched on or off explicitly. They default to
# 'auto' in meson.options, which quietly turns a feature on if its -dev
# package happens to be installed, so the image would depend on the build
# environment rather than on this file. Setting them here makes 'enabled' a
# decision, and a missing dependency fails the build instead of quietly
# leaving the feature out.
#
# MySQL and PostgreSQL add about 1 MB to the runtime image (mariadb-connector-c
# and libpq) and cost nothing if unused, so they're built in. NETCONF and
# Kerberos stay off: both add a lot more, for features few people here need.
#
# install_umask=0022 is needed for Kea 3.0, and harmless for 3.2.
#
# Kea 3.0.x sets 'install_umask=0027' in its project() default_options, which
# installs every library as 0750 root:root. Our servers run as UID 10000 and
# can't read them, so kea-dhcp4 stops at startup with
# "Error loading shared library libkea-util.so.103: Permission denied".
# ISC's images don't hit this because they run as root.
#
# Kea 3.2 replaced the global umask with install_mode on its state directories,
# so it already installs 0755. Setting it here gives both branches the same,
# intended permissions. See D14.
RUN set -eux; \
    meson setup build \
        --prefix=/usr \
        --sysconfdir=/etc \
        --localstatedir=/var \
        --buildtype=release \
        -D runstatedir=/run \
        -D crypto=openssl \
        -D cpp_std=c++17 \
        -D install_umask=0022 \
        -D mysql=enabled \
        -D postgresql=enabled \
        -D netconf=disabled \
        -D krb5=disabled \
        -D tests=disabled \
        -D fuzz=disabled; \
    meson compile -C build -j "${BUILD_JOBS}"; \
    DESTDIR=/stage meson install -C build --no-rebuild --quiet; \
    install -D -m 0644 COPYING /stage/usr/share/licenses/kea/COPYING; \
    install -D -m 0644 AUTHORS /stage/usr/share/licenses/kea/AUTHORS

###############################################################################
# db-clients - where the two command-line clients kea-admin uses come from
#
# kea-admin calls `mysql` (in 30 places) and `psql` (in 5). The full
# mariadb-client package is 65 MB, because it's seven similar ~5 MB tools
# (mariadb-dump, mariadb-check, mariadb-import, ...) that kea-admin never uses.
# Copying just the two it needs costs 8.8 MB.
#
# Their other dependencies (libcrypto, libssl, libstdc++, libz, libgcc) are
# already in runtime-base, so they add nothing.
###############################################################################
FROM ${ALPINE_REF} AS db-clients
# pipefail matters below: without it, a failing psql or apk in a pipeline
# would be hidden by a successful sed, and the version check would pass.
SHELL ["/bin/ash", "-o", "pipefail", "-c"]
# Alpine carries postgresql16, 17 and 18 side by side and changes the
# recommended one over time, so a hardcoded version would go stale. Match it
# to the libpq that runtime-base installs, and check that they match: a psql
# that disagrees with the library the servers use would be confusing for
# someone already debugging a database problem.
RUN set -eux; \
    apk add --no-cache mariadb-client postgresql18-client; \
    psql_major="$(psql --version | sed -nE 's/.* ([0-9]+)\..*/\1/p')"; \
    libpq_major="$(apk list --installed libpq | sed -nE 's/^libpq-([0-9]+)\..*/\1/p' | head -1)"; \
    echo "psql major=${psql_major}  libpq major=${libpq_major}"; \
    test -n "${psql_major}"; \
    test "${psql_major}" = "${libpq_major}"

###############################################################################
# runtime-base - one shared layer behind every published image
#
# The runtime libraries, Kea's own libkea-*.so libraries and the open-source
# hook libraries. Every image below starts FROM this stage, so the registry
# stores these layers once, and pulling all five images downloads them once.
# No build tools are included.
###############################################################################
FROM ${ALPINE_REF} AS runtime-base

# The UID and GID are fixed and documented, so people can chown bind-mounted
# lease directories ahead of time. Changing them would break existing setups.
ARG KEA_UID=10000
ARG KEA_GID=10000

ARG KEA_VERSION
ARG SOURCE_URL
ARG REVISION
ARG CREATED

# org.opencontainers.image.source links each published GHCR package back to
# this repository. Without it, the package isn't linked, which affects both
# how people find it and which permissions it inherits. See D15.
#
# The description and vendor say these are unofficial community builds, so
# that note travels with the image, not just the README.
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.documentation="${SOURCE_URL}#readme" \
      org.opencontainers.image.licenses="MPL-2.0" \
      org.opencontainers.image.version="${KEA_VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.vendor="dapalab (unofficial community build)" \
      org.opencontainers.image.description="Unofficial multi-arch container image for ISC Kea DHCP ${KEA_VERSION}, built from PGP-verified upstream source. Not affiliated with or endorsed by Internet Systems Consortium."

RUN set -eux; \
    apk add --no-cache libstdc++ log4cplus libcrypto3 libssl3 tzdata \
        netcat-openbsd mariadb-connector-c libpq; \
    addgroup -S -g "${KEA_GID}" kea; \
    adduser -S -u "${KEA_UID}" -G kea -H -s /sbin/nologin kea; \
    mkdir -p /etc/kea /var/lib/kea /run/kea; \
    chown kea:kea /var/lib/kea /run/kea; \
    # Kea 3.x won't use a Unix control socket whose directory is more open
    # than 0750, and is just as strict about the lease directory. mkdir's
    # default of 0755 is rejected when the config is read.
    chmod 0750 /run/kea /var/lib/kea

# Kea's libraries and hook libraries, plus the licence files that go with the
# binaries (Kea is MPL-2.0).
COPY --from=builder /stage/usr/lib/ /usr/lib/
COPY --from=builder /stage/usr/share/licenses/kea/ /usr/share/licenses/kea/
# Kea is compiled from source, so buildx's SBOM scanner, which lists what the
# package manager installed, can't see it. This document is what puts Kea in
# the published SBOM. See build/make-sbom.sh and D23.
COPY --from=builder /stage/usr/share/sbom/ /usr/share/sbom/

###############################################################################
# kea-dhcp4
#
# kea-lfc is included because memfile leases need it: kea-dhcp4 runs it to
# tidy up the lease file. If it's missing, kea-dhcp4 won't start; if it's
# present but broken, Kea doesn't notice, which is why smoke gate 6 checks
# that the lease file really is compacted (D22).
#
# CAP_NET_RAW            a raw socket, to hear DHCPv4 broadcasts
# CAP_NET_BIND_SERVICE   binding UDP port 67
#
# The capabilities are set on the binary itself, so the server can run as a
# non-root user and still use them. Both are in Docker's default set; with
# --cap-drop=ALL, add them back explicitly.
###############################################################################
FROM runtime-base AS dhcp4
LABEL org.opencontainers.image.title="kea-dhcp4"
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
# kea-dhcp6 - CAP_NET_BIND_SERVICE only, since DHCPv6 uses UDP multicast
###############################################################################
FROM runtime-base AS dhcp6
LABEL org.opencontainers.image.title="kea-dhcp6"
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
# kea-dhcp-ddns - no capabilities needed; ports 53001 and 8000 are above 1024
###############################################################################
FROM runtime-base AS dhcp-ddns
LABEL org.opencontainers.image.title="kea-dhcp-ddns"
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
# kea-ctrl-agent  (Kea 3.0 only)
#
# Kea 3.2 removed the agent, because the servers now have their own HTTP/TLS
# control sockets. versions.json lists images per branch so that this stage is
# only built for 3.0. Built against 3.2, it fails at the COPY below, which is
# the clear failure we'd want.
#
# No capabilities needed: port 8000 is above 1024.
#
# It shares /run/kea with the servers it talks to, so /run/kea is a declared
# volume rather than a private directory.
###############################################################################
FROM runtime-base AS ctrl-agent
LABEL org.opencontainers.image.title="kea-ctrl-agent"
COPY --from=builder /stage/usr/sbin/kea-ctrl-agent /usr/sbin/kea-ctrl-agent
COPY config/kea-ctrl-agent.conf /etc/kea/kea-ctrl-agent.conf

# The agent speaks HTTP rather than a Unix socket, so the check has to be a
# real HTTP request; sending it bare JSON doesn't work.
#
# Any HTTP status line counts as healthy, on purpose: a 401 from an agent with
# authentication turned on still shows it's up and answering, and the
# healthcheck shouldn't need credentials.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "b='{\"command\":\"version-get\"}'; printf 'POST / HTTP/1.0\\r\\nContent-Type: application/json\\r\\nContent-Length: %d\\r\\n\\r\\n%s' ${#b} \"$b\" | nc -w3 127.0.0.1 8000 | grep -q '^HTTP/'"]
USER 10000:10000
EXPOSE 8000/tcp
VOLUME ["/etc/kea", "/run/kea"]
CMD ["/usr/sbin/kea-ctrl-agent", "-c", "/etc/kea/kea-ctrl-agent.conf"]

###############################################################################
# kea-tools - kea-admin, perfdhcp, keactrl, kea-lfc
#
# Not a server. It gives people the admin tools ISC's images leave out (their
# issue #46: "database schema update impossible because kea-admin missing"),
# and gives CI a proper way to run the perfdhcp functional test.
#
# kea-admin comes with the mysql and psql clients it calls, and with the
# schema SQL, so it can actually do its job.
#
# kea-shell is left out on purpose. It's a standalone Python client for the
# control API, and nothing depends on it: HA runs inside the servers via
# libdhcp_ha.so, and DDNS requests go from the servers to kea-dhcp-ddns over
# UDP. Including it would bring in python3 and roughly double the image size,
# and the netcat in every image reaches the same API. See
# docs/configuration.md.
#
# perfdhcp gets CAP_NET_RAW so it can send DHCPv4 test packets as a non-root
# user.
###############################################################################
FROM runtime-base AS tools
LABEL org.opencontainers.image.title="kea-tools"
COPY --from=builder /stage/usr/sbin/perfdhcp   /usr/sbin/perfdhcp
COPY --from=builder /stage/usr/sbin/kea-lfc    /usr/sbin/kea-lfc
COPY --from=builder /stage/usr/sbin/kea-admin  /usr/sbin/kea-admin
COPY --from=builder /stage/usr/sbin/keactrl    /usr/sbin/keactrl
# Schema creation and upgrade scripts for the database backends: small plain
# SQL files, and what people need when setting up a MySQL or PostgreSQL lease
# database.
COPY --from=builder /stage/usr/share/kea/scripts/ /usr/share/kea/scripts/

# The command-line clients kea-admin needs. `mysql` is MariaDB's
# compatibility name for the same binary; it prints a deprecation notice on
# stderr only, so kea-admin's parsing of stdout isn't affected.
RUN apk add --no-cache libncursesw readline
COPY --from=db-clients /usr/bin/mariadb /usr/bin/mariadb
COPY --from=db-clients /usr/bin/psql    /usr/bin/psql
RUN ln -sf /usr/bin/mariadb /usr/bin/mysql
RUN set -eux; \
    apk add --no-cache --virtual .setcap libcap; \
    setcap cap_net_raw,cap_net_bind_service=+ep /usr/sbin/perfdhcp; \
    apk del .setcap
USER 10000:10000
CMD ["/usr/sbin/perfdhcp", "-h"]
