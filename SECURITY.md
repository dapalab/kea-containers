# Security policy

Thanks for taking the time to report a problem. This is a community project,
not an ISC one, so where to report depends on what you've found.

## Problems with these images

Report these here, privately:
**[Report a vulnerability](https://github.com/dapalab/kea-containers/security/advisories/new)**

That includes anything in how the images are built, configured, signed or
published, for example:

- a default configuration, file permission or capability that's less safe
  than it should be
- a way to get an image published that didn't come from this repository's
  build, or that bypasses the source signature check
- a problem in one of this repository's workflows or scripts

Please don't open a public issue for these until there's a fix.

## Problems in Kea itself

If the problem would affect Kea wherever it runs, not just in these images,
please report it to ISC, who maintain Kea. Their instructions are at
<https://www.isc.org/reportbug/>. Once ISC publishes a fix, these images pick
it up with the next Kea release.

## Known vulnerabilities in Alpine packages

You don't need to report these. The published images are scanned every day,
and after every build, and anything rated High or above opens an issue here
automatically. The images are rebuilt weekly to pick up Alpine's fixes. If an
image still carries a known vulnerability more than a week after Alpine has
fixed it, that's worth reporting here.

## Which images are supported

| Tag | Supported |
|---|---|
| `3.2` (and its stamped tags) | yes |
| `3.0`, `3.0-lts` (and their stamped tags) | yes |
| anything else | no |

Only the newest build of each branch gets fixes. A stamped tag such as
`3.2.0-20260920-2244` never changes, so it won't receive fixes; move to a newer
build to get them. See
[Tags and updates](https://github.com/dapalab/kea-containers/blob/main/docs/tags-and-updates.md).

To check an image really came from this repository, see
[verifying these images](https://github.com/dapalab/kea-containers/blob/main/docs/image-security.md#checking-an-image-came-from-here).
