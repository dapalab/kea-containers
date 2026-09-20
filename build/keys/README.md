# Vendored ISC code-signing keys

`isc-keyblock.asc` is the keyring the build verifies the Kea source tarball
against. It is vendored deliberately: fetching it at build time would let an
attacker who controls the download path supply both a tarball and a key that
matches it. See `docs/DECISIONS.md` D2.

Because re-vendoring is a deliberate act, it needs something to review
*against*. That is what this file is.

## Keys in the block

Recorded **2026-09-20**. Seven keys, all `[SC]`, **none carrying an expiry
date** and none revoked at the time of recording.

| Fingerprint | User ID | Created |
|---|---|---|
| `706B6C28620E76F91D11F7DF510A642A06C52CEC` | Michał Kępień `<michal@isc.org>` | 2022-11-03 |
| `D99CCEAF879747014F038D63182E23579462EFAA` | Michal Nowak `<mnowak@isc.org>` | 2022-11-03 |
| `0259A33B5F5A3A4466CF345C7A5E084CACA51884` | Wlodek Wencel `<wlodek@isc.org>` | 2022-11-03 |
| `090A2A07923F925B5767803A42E5DF78C83271DB` | Marcin Godzina `<mgodzina@isc.org>` | 2022-11-03 |
| `9580D6BF2CC80F1E3BB11252DEAB91D54B13C9B8` | Greg Choules `<greg@isc.org>` | 2022-11-03 |
| `FC874C3E3FE8677070AC71BEB5EFF6AC7E1ADDF8` | Cathy Almond `<cathya@isc.org>` | 2022-11-03 |
| `DA6A3508E672A49DD382AFD95B8F4D91B88ED909` | Andrei Pavel `<andrei@isc.org>` | 2023-04-27 |

Different releases are signed by different people — 3.2.0 by Andrei Pavel,
3.0.4 by Wlodek Wencel — which is why no single fingerprint can be pinned.

## How this block was checked

On 2026-09-20 the vendored file was compared against
<https://www.isc.org/docs/isc-keyblock.asc> — a different host to
`downloads.isc.org`, which serves the tarballs, so a compromise of the
download path alone does not also control this:

```bash
curl -fsSL https://www.isc.org/docs/isc-keyblock.asc -o /tmp/upstream.asc
cmp build/keys/isc-keyblock.asc /tmp/upstream.asc && echo "byte-identical"
```

Result: **byte-identical**, 11190 bytes, seven keys.

A keyserver cross-check is only partially useful here. Of the seven keys, only
`0259A33B…` (Wlodek Wencel) is published on `keys.openpgp.org`; the other six
return 404, because that keyserver requires per-address opt-in rather than
because anything is wrong:

```bash
curl -sI "https://keys.openpgp.org/vks/v1/by-fingerprint/<FPR>"
```

## Re-vendoring

Builds fail closed when ISC signs a release with a key outside this block, so
re-vendoring is forced on you at exactly the right moment. When it happens:

1. Fetch the new block from `https://www.isc.org/docs/isc-keyblock.asc`.
2. Diff the key set against the table above — `gpg --show-keys` on both.
3. Confirm the *added* key out of band. Do not simply accept it because the
   tarball it signs verifies against it; that is circular.
4. Update the table, the date, and the recorded check above.
5. Commit the keyblock and this file together, in their own commit.

`scripts/check-upstream.py` reports on this block weekly: it fails loudly if
any key is revoked or expired, and warns at 90 days before an expiry.
