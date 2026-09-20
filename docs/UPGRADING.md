# Upgrading

Moving between Kea branches — 3.0 (LTS) to 3.2, or the reverse. Everything
below was run against these images rather than taken from documentation;
where a number appears, it was observed.

**If you use `memfile` (the default), there is nothing to do.** Change the
tag and restart. The lease CSV format is compatible and `kea-lfc` handles it.
The rest of this page is only about MySQL and PostgreSQL.

---

## The short version

```bash
# 1. stop the daemons  2. back up  3. upgrade the schema  4. start the new tag
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin db-upgrade mysql -h <host> -u <user> -p <password> -n <database>
```

Skipping step 3 does not corrupt anything — the new daemon refuses to start.

---

## Why a schema upgrade is needed at all

Each Kea branch expects an exact database schema version. Observed by
initialising a fresh database with each branch's `kea-admin`:

| | MySQL schema | PostgreSQL schema |
|---|---|---|
| Kea 3.0.4 (LTS) | **30.0** | **29.0** |
| Kea 3.2.0 | **35.0** | **34.0** |

So 3.0 → 3.2 is a five-step migration in both backends. It is not a
formality.

## What happens if you skip it

The daemon **refuses to start**, with the exact numbers in the message:

```
ERROR [kea-dhcp4.dhcp4] DHCP4_CONFIG_LOAD_FAIL configuration error using file:
  /etc/kea/kea-dhcp4.conf, reason: Unable to open database:
  MySQL schema version mismatch: expected version: 35.0, found version: 30.0
ERROR [kea-dhcp4.dhcp4] DHCP4_INIT_FAIL failed to initialize Kea server
```

The container exits `1`. Nothing is written, nothing is migrated halfway, and
no leases are served from a mismatched schema. This is the good case: it
fails loudly and immediately rather than running degraded.

Under an orchestrator the pod will crash-loop, which is the same signal.

## The upgrade is one-way

**You cannot roll back by putting the old tag back.** The old daemon checks
the schema just as strictly, in the other direction:

```
MySQL schema version mismatch: expected version: 30.0, found version: 35.0
```

`kea-admin` has **no `db-downgrade` command** — the supported operations are
`db-init`, `db-version`, `db-upgrade`, `lease-dump`, `lease-upload` and
`stats-recount`. Rolling back means restoring the backup. Which is why the
backup is step 2 and not an optional extra.

---

## Back up first — and not with `kea-tools`

`kea-tools` deliberately ships only the `mysql` and `psql` **clients**, the
two binaries `kea-admin` shells out to. `mysqldump`, `mariadb-dump`,
`pg_dump` and `pg_dumpall` are **not present** — including them would add
~65 MB of near-identical binaries nothing calls (see `docs/DECISIONS.md`
D13).

A naive `docker run … kea-tools … mysqldump` therefore produces an **empty
file and no error**, which is a bad way to discover your backup does not
exist. Use the database's own image:

```bash
# MySQL / MariaDB
docker run --rm --network <net> mariadb:11 \
  mariadb-dump -h <host> -u <user> -p<password> <database> > kea-backup.sql

# PostgreSQL
docker run --rm --network <net> postgres:18 \
  pg_dump -h <host> -U <user> -d <database> > kea-backup.sql
```

Verify it is not empty before going further:

```bash
test -s kea-backup.sql && grep -c 'CREATE TABLE' kea-backup.sql
```

A full schema dump of a Kea database is around 63 tables.

**A lease-only export**, which `kea-tools` *can* do, is a useful extra but is
not a substitute for the dump above — it captures leases, not hosts,
reservations, options or statistics:

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin lease-dump mysql -h <host> -u <user> -p <password> -n <db> \
    -4 -o /tmp/leases4.csv
```

---

## The upgrade, step by step

### 1. Stop every daemon that uses the database

Both `kea-dhcp4` and `kea-dhcp6` may share it, and so may a second host in an
HA pair. Upgrading underneath a running daemon is not supported.

```bash
docker stop kea4 kea6
```

### 2. Back up

As above. Check the file is non-empty.

### 3. Check where you are

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.0 \
  kea-admin db-version mysql -h <host> -u <user> -p <password> -n <db>
# -> 30.0
```

### 4. Upgrade, using the tools image of the version you are moving **to**

The upgrade scripts ship inside the image, so the 3.2 tools are what know how
to reach 35.0:

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin db-upgrade mysql -h <host> -u <user> -p <password> -n <db>
```

Observed output on a real 30.0 → 35.0 run:

```
Processing /usr/share/kea/scripts/mysql/upgrade_030_to_031.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_031_to_032.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_032_to_033.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_033_to_034.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_034_to_035.sh file...
Schema version reported after initialization: 35.0
```

For PostgreSQL, substitute `pgsql` for `mysql`; the flags are identical.

### 5. Confirm, then start the new tag

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin db-version mysql -h <host> -u <user> -p <password> -n <db>
# -> 35.0
```

Then start `ghcr.io/dapalab/kea-dhcp4:3.2` and confirm `DHCP4_STARTED` in the
log. A schema problem would have stopped it before that line.

---

## Passwords on the command line

Every example above puts the password in `argv`, where it is visible to
`docker inspect`, `ps` and your shell history. Kea supports reading it from a
file instead, which is what a real deployment should use:

```json
"lease-database": {
  "type": "mysql",
  "host": "db.example.net",
  "name": "kea",
  "user": "kea",
  "password-file": "/run/secrets/kea-db-password"
}
```

`kea-admin` still wants `-p` on the command line, so run it from somewhere
the argument does not persist — not from an interactive shell whose history
is saved.

---

## Which direction, and when

Adding or retiring a branch is a single entry in `versions.json`, and the
tagging policy makes moving between branches a deliberate act rather than
something that happens to you (`docs/DECISIONS.md` D10). That is exactly the
point at which this page matters: the tag change is trivial and the schema
step is the one people forget.

3.0 is the LTS and outlives 3.2 by eleven months. Moving 3.2 → 3.0 is a
legitimate choice for stability, and it is a **downgrade** in schema terms —
which, per above, means restoring a backup rather than running a command.
Plan it as a migration, not a rollback.
