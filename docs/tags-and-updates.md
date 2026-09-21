# Tags and updates

## What each tag means

| Tag | Changes over time? | What it points at |
|---|---|---|
| `3.2` | yes | The newest build of the 3.2 stable branch |
| `3.0` | yes | The newest build of the 3.0 branch |
| `3.0-lts` | yes | Same as `3.0`: ISC's long-term support branch |
| `3.2.0` | yes | The newest build of Kea 3.2.0 exactly |
| `3.2.0-20260920-2244` | **never** | Kea 3.2.0 as built at that minute (UTC) |

## Why tags get rebuilt

Every image is rebuilt each week, and again whenever one of its dependencies
is updated. Kea itself stays the same, but the Alpine packages underneath it
pick up their latest security fixes. That's how fixes reach you in between
Kea releases.

So `3.2.0` always contains Kea 3.2.0, but it may not be the exact image you
pulled last month. If you need an image that will never change, use the
stamped tag (the one ending in a date and time) or a digest.

## Which tag should you use?

**If a bot manages your updates** (Renovate, Dependabot and the like), use a
moving tag and let the bot pin the digest:

```
ghcr.io/dapalab/kea-dhcp4:3.2@sha256:...
```

The digest means every deployment is exactly reproducible, and the bot opens a
pull request whenever there's a new build or a new version. For Renovate:

```json5
{ "packageRules": [{ "matchPackageNames": ["ghcr.io/dapalab/kea-**"],
                     "pinDigests": true }] }
```

**If you update by hand**, the stamped tag is the friendliest pin:

```
ghcr.io/dapalab/kea-dhcp4:3.2.0-20260920-2244
```

It never changes, and unlike a digest you can read it at a glance. Just
remember that nothing will prompt you to move on from it, so you won't get
security rebuilds until you update it yourself. A calendar reminder helps.

## Why there's no `latest` or `3` tag

A DHCP server quietly jumping to a new version isn't something most people
want, so there's no `latest`. There's no bare `3` either, because it would be
misleading. It would point at 3.2, but 3.0 is the long-term support branch and
is supported for about eleven months longer. Anyone choosing `3` for
stability would get the shorter-lived one.

## Renovate and the stamped tags

Renovate can't follow the stamped tags with its standard settings. Its
`docker` versioning treats everything after the first `-` as a
"compatibility" label, and only offers updates with the same label. Since
every build has a new stamp, it never finds one, and it doesn't warn you
about that either.

| Pinned on | Renovate offers |
|---|---|
| `3.2.0` | `3.2.1` when it's out |
| `3.2` | `3.4` when it's out |
| `3.2.0-20260920-2244` | nothing, ever |

That's why the moving tag plus a digest is the best fit for automation. If
you'd still like a bot to track stamped tags, `regex` versioning works:

```json5
{
  "matchPackageNames": ["ghcr.io/dapalab/kea-**"],
  "versioning": "regex:^(?<major>\\d+)\\.(?<minor>\\d+)\\.(?<patch>\\d+)-(?<build>\\d+)-(?<revision>\\d+)$",
  "allowedVersions": "<3.3.0"
}
```

Keep the `allowedVersions` line. Without it, Renovate will happily suggest
moving from `3.0.4-…` to `3.2.0-…`, which takes you off the long-term support
branch.

## Moving between 3.0 and 3.2

Changing branch is always your decision; no tag will do it for you. If you
store leases in the default CSV file, it's just a tag change. With MySQL or
PostgreSQL there's a schema migration to do first: see
[Upgrading](upgrading.md).
