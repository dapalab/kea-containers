# Security

## Non-root by default

The servers run as an ordinary user, `kea` (UID and GID 10000), not as root.
The few extra permissions each one needs are attached to its program file as
Linux *file capabilities*, so everything works with Docker's defaults. You
don't need `--cap-add`, and you never need `--privileged`.

| Image | Capabilities | Why |
|---|---|---|
| `kea-dhcp4` | `NET_RAW`, `NET_BIND_SERVICE` | A raw socket to hear DHCPv4 broadcasts; binding UDP port 67 |
| `kea-dhcp6` | `NET_BIND_SERVICE` | Binding UDP port 547 |
| `kea-dhcp-ddns` | none | Its ports are all above 1024 |
| `kea-ctrl-agent` | none | Its port is above 1024 |
| `kea-tools` | `NET_RAW` | `perfdhcp` sends raw test packets |

`kea-dhcp4` really does need both. With `NET_RAW` alone, it won't start.

### Locking things down further

If you drop all capabilities (a good habit), add back the ones the image
needs:

```bash
docker run --cap-drop=ALL \
  --cap-add=NET_RAW --cap-add=NET_BIND_SERVICE \
  ghcr.io/dapalab/kea-dhcp4:3.2
```

If you forget, the container fails straight away with
`exec: operation not permitted`, so it's easy to spot.

A detail that sometimes trips people up: in Docker, `--cap-add` on its own
doesn't hand a capability to a non-root process. It only makes the
capability *available* to the container. It's the file capabilities built
into these images that let the `kea` user actually use them.

On Kubernetes, `allowPrivilegeEscalation: false` works fine with these
images. See [Kubernetes](kubernetes.md).

## Checking an image came from here

Every image is signed with [cosign](https://docs.sigstore.dev/) using
*keyless* signing. There's no private key that could leak. Instead, each
signature is tied to the GitHub Actions workflow that built the image and
recorded in Sigstore's public transparency log.

To check one:

```bash
cosign verify ghcr.io/dapalab/kea-dhcp4:3.2 \
  --certificate-identity-regexp \
    '^https://github\.com/dapalab/kea-containers/\.github/workflows/build\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Don't have cosign installed? Its official image works just as well, on arm64
too, and doesn't need access to your Docker socket:

```bash
docker run --rm ghcr.io/sigstore/cosign/cosign:latest verify \
  ghcr.io/dapalab/kea-dhcp4:3.2 \
  --certificate-identity-regexp \
    '^https://github\.com/dapalab/kea-containers/\.github/workflows/build\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

A successful check means these exact bytes were built by this repository's
`build.yml` workflow. If someone published a modified copy under a similar
name, it would fail.

Keep the `--certificate-identity-regexp` part. Without it, the check only
proves that *someone* signed the image, not that this project did.

**Pinned to a single architecture's digest?** That verifies too. Each
multi-arch image is signed, and so are its `linux/amd64` and `linux/arm64`
images, so `ghcr.io/dapalab/kea-dhcp4@sha256:<arm64 digest>` works with the
same command.

## What's inside: SBOM and provenance

Each image carries a software bill of materials (SBOM) and a build provenance
record:

```bash
docker buildx imagetools inspect ghcr.io/dapalab/kea-dhcp4:3.2 \
  --format '{{ json .SBOM }}'

docker buildx imagetools inspect ghcr.io/dapalab/kea-dhcp4:3.2 \
  --format '{{ json .Provenance }}'
```

The SBOM lists Kea itself, with its version, licence and source checksum,
alongside the Alpine packages. To pick Kea out:

```bash
docker buildx imagetools inspect ghcr.io/dapalab/kea-dhcp4:3.2 \
  --format '{{ json .SBOM }}' | jq '[.. | objects | select(.name? == "kea")][0]'
```

The published images are scanned for known vulnerabilities every day with
[grype](https://github.com/anchore/grype), and an issue is opened in this
repository if anything High or above turns up. You can run the same scan
yourself:

```bash
docker run --rm anchore/grype:v0.119.0 ghcr.io/dapalab/kea-dhcp4:3.2
```

## How the build is checked

Here's how trust passes from ISC to you:

1. **ISC signs the Kea source code.** The build checks that signature against
   a copy of ISC's signing keys stored in this repository, and stops if it
   doesn't match. The keys are stored here, not downloaded during the build,
   so nobody who could tamper with the download could also swap in a
   matching key. Their fingerprints, and where they were checked, are in
   [`build/keys/README.md`](../build/keys/README.md).
2. **The build runs tests before publishing anything**, including a real DHCP
   exchange with 20 simulated clients.
3. **The published image is signed** by the build workflow.
4. **You check that signature**, as above.

Each step can be checked without trusting any of the others.

The signature check in step 1 is stricter than gpg's own exit code, which
reports success even for a key that has expired or been revoked. The reasons,
and the tests that prove it, are in
[Behind the scenes](DECISIONS.md) (D2).

ISC publishes signatures for Kea but no checksum files, so the signature is
the only upstream check there is.

## What the signature doesn't tell you

A valid signature proves where an image came from and that it hasn't been
changed since. It doesn't mean ISC has reviewed or endorsed these images.
They're a community project. For an unofficial image, though, "who actually
built this?" is exactly the question you most want answered, and the
signature answers it.
