#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Emit an SPDX document describing Kea itself.
#
#   usage: make-sbom.sh <version> <tarball> <url> <output.spdx.json>
#
# Why this exists
#     buildx --sbom=true lists what the package manager installed. Kea isn't
#     installed by apk (it's compiled from source and copied out of a build
#     stage), so it didn't appear. On the published kea-dhcp4:3.2 before this
#     existed: 25 apk packages listed, and no mention of Kea at all, even
#     though the image contains 23 libkea-*.so libraries, the server and a
#     dozen hook libraries. See D23.
#
# How it gets into the published SBOM
#     BuildKit's syft scanner reads SBOM documents it finds in the image and
#     merges them into the SBOM it publishes. Tested with a local build: with
#     this document at /usr/share/sbom/kea.spdx.json, the published SBOM
#     gains a `kea` package with its version, MPL-2.0, the upstream URL, the
#     tarball checksum and a CPE (cpe:2.3:a:isc:kea:<version>). The CPE is
#     what lets a vulnerability scanner match Kea's CVEs.
#
# A good home for the tarball's hash
#     D2 removed a bare `sha256sum` from the verification step, because it
#     compared the hash with nothing. Recording the same hash here, as the
#     checksum of the source the binaries were built from, is a genuine use
#     for it: a record of where the image came from, not a check.
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

# SOURCE_DATE_EPOCH keeps this byte-identical in the test build and the push
# build. A live timestamp would change the layer and fail build.yml's check
# that the published image is the tested one (D21).
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

# Invalid JSON would fail silently: the scanner skips what it can't parse,
# and Kea would drop out of the SBOM with no error. So read it back before
# reporting success.
if command -v python3 >/dev/null 2>&1; then
    python3 -c "import json,sys; d=json.load(open(sys.argv[1])); \
        p=d['packages'][0]; assert p['name']=='kea', p; \
        assert p['versionInfo']==sys.argv[2], p" "$out" "$version" \
        || { echo "FATAL: generated SBOM is not valid or not what we meant" >&2; exit 1; }
fi

echo "--- SBOM: kea ${version} sha256:${sha} -> ${out} ---"
