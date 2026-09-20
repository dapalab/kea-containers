#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Emit an SPDX document describing Kea itself.
#
#   usage: make-sbom.sh <version> <tarball> <url> <output.spdx.json>
#
# WHY THIS EXISTS
#     buildx --sbom=true catalogues the package manager's view of the image.
#     Kea is not installed by apk - it is compiled from source and copied out
#     of a builder stage - so it does not appear. Measured on the published
#     kea-dhcp4:3.2 before this existed: 25 apk packages catalogued, and the
#     string "kea" absent from the SBOM entirely, while the image ships 23
#     libkea-*.so libraries, the daemon and a dozen hook libraries.
#
#     An SBOM that lists busybox and tzdata but not the DHCP server is worse
#     than no SBOM, because it reads as diligence.
#
# HOW IT LANDS IN THE ATTESTATION
#     BuildKit's syft scanner reads SBOM documents that are present in the
#     image and merges them into the attestation it produces. Verified
#     against a local build: with this document at
#     /usr/share/sbom/kea.spdx.json the published SBOM gains a `kea` package
#     with its version, MPL-2.0, the upstream URL, the tarball checksum and
#     a synthesised CPE (cpe:2.3:a:kea:kea:<version>) - which is also what
#     lets a vulnerability scanner match Kea CVEs at all.
#
# THE CHECKSUM IS THE HONEST USE OF A HASH
#     D2 removed a bare `sha256sum` from the verification block because it
#     compared the hash to nothing. Recording that same hash HERE, as the
#     checksum of the source the binaries were built from, is the use that
#     was always worth having: it is a statement about provenance, not a
#     check pretending to be one.
set -eu

version="${1:?usage: make-sbom.sh <version> <tarball> <url> <output>}"
tarball="${2:?usage: make-sbom.sh <version> <tarball> <url> <output>}"
url="${3:?usage: make-sbom.sh <version> <tarball> <url> <output>}"
out="${4:?usage: make-sbom.sh <version> <tarball> <url> <output>}"

case "$version" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "FATAL: version must look like X.Y.Z, got '$version'" >&2; exit 1 ;;
esac
[ -f "$tarball" ] || { echo "FATAL: no such tarball: $tarball" >&2; exit 1; }

sha="$(sha256sum "$tarball" | cut -d' ' -f1)"
[ -n "$sha" ] || { echo "FATAL: could not hash $tarball" >&2; exit 1; }

# SOURCE_DATE_EPOCH keeps this byte-identical across the test build and the
# push build; a timestamp here would change the layer and break the
# tested-equals-published assertion in build.yml (D21).
created="$(date -u -d "@${SOURCE_DATE_EPOCH:-0}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
           || echo "1970-01-01T00:00:00Z")"

mkdir -p "$(dirname "$out")"
cat > "$out" <<JSON
{
  "spdxVersion": "SPDX-2.3",
  "SPDXID": "SPDXRef-DOCUMENT",
  "name": "kea-${version}",
  "dataLicense": "CC0-1.0",
  "documentNamespace": "https://github.com/dapalab/kea-containers/spdx/kea-${version}-${sha}",
  "creationInfo": {
    "created": "${created}",
    "creators": [
      "Tool: kea-containers-make-sbom",
      "Organization: dapalab (unofficial community build)"
    ]
  },
  "packages": [
    {
      "SPDXID": "SPDXRef-Package-kea",
      "name": "kea",
      "versionInfo": "${version}",
      "supplier": "Organization: Internet Systems Consortium (ISC)",
      "originator": "Organization: Internet Systems Consortium (ISC)",
      "downloadLocation": "${url}",
      "sourceInfo": "Compiled from the ISC source tarball, whose detached PGP signature was verified against a vendored copy of ISC's code-signing keyblock.",
      "filesAnalyzed": false,
      "licenseConcluded": "MPL-2.0",
      "licenseDeclared": "MPL-2.0",
      "copyrightText": "Copyright (C) Internet Systems Consortium, Inc. (\"ISC\")",
      "checksums": [
        { "algorithm": "SHA256", "checksumValue": "${sha}" }
      ],
      "externalRefs": [
        {
          "referenceCategory": "SECURITY",
          "referenceType": "cpe23Type",
          "referenceLocator": "cpe:2.3:a:isc:kea:${version}:*:*:*:*:*:*:*"
        },
        {
          "referenceCategory": "PACKAGE-MANAGER",
          "referenceType": "purl",
          "referenceLocator": "pkg:generic/kea@${version}?download_url=${url}&checksum=sha256:${sha}"
        }
      ]
    }
  ],
  "relationships": [
    {
      "spdxElementId": "SPDXRef-DOCUMENT",
      "relatedSpdxElement": "SPDXRef-Package-kea",
      "relationshipType": "DESCRIBES"
    }
  ]
}
JSON

# Emitting invalid JSON would be silent: the scanner skips what it cannot
# parse, and the SBOM would go back to omitting Kea with nothing to show for
# it. Parse it back before declaring success.
if command -v python3 >/dev/null 2>&1; then
    python3 -c "import json,sys; d=json.load(open(sys.argv[1])); \
        p=d['packages'][0]; assert p['name']=='kea', p; \
        assert p['versionInfo']==sys.argv[2], p" "$out" "$version" \
        || { echo "FATAL: generated SBOM is not valid or not what we meant" >&2; exit 1; }
fi

echo "--- SBOM: kea ${version} sha256:${sha} -> ${out} ---"
