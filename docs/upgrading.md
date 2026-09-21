# Upgrading between 3.0 and 3.2

**If your leases are stored in a file (the default), there's nothing special
to do.** Change the tag and restart. The lease file format is the same in both
branches.

The rest of this page is for MySQL and PostgreSQL users. The commands and
output below all come from real runs against these images.

## In short

1. Stop the Kea servers.
2. Back up the database.
3. Upgrade the schema with the `kea-tools` image for the version you're moving
   **to**.
4. Start the new tag.

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin db-upgrade mysql -h <host> -u <user> -p <password> -n <database>
```

If you forget step 3, nothing breaks: the new server just refuses to start
until the schema is upgraded.

## Why the schema needs upgrading

Each Kea branch expects one exact database schema version:

| | MySQL schema | PostgreSQL schema |
|---|---|---|
| Kea 3.0.4 (LTS) | 30.0 | 29.0 |
| Kea 3.2.0 | 35.0 | 34.0 |

So moving from 3.0 to 3.2 is five schema steps on either database.
`kea-admin` runs them all in one go.

## If you skip it

The server stops at startup and tells you exactly what it expected:

```
ERROR [kea-dhcp4.dhcp4] DHCP4_CONFIG_LOAD_FAIL configuration error using file:
  /etc/kea/kea-dhcp4.conf, reason: Unable to open database:
  MySQL schema version mismatch: expected version: 35.0, found version: 30.0
ERROR [kea-dhcp4.dhcp4] DHCP4_INIT_FAIL failed to initialize Kea server
```

The container exits with code 1 (on Kubernetes, the pod will restart in a
loop). Nothing has been written and nothing is half-migrated, so you can
simply run the upgrade and start it again.

## There's no automatic way back

Once the schema is upgraded, the old version won't start against it. It checks
the version just as strictly:

```
MySQL schema version mismatch: expected version: 30.0, found version: 35.0
```

`kea-admin` can't downgrade a schema. It supports `db-init`, `db-version`,
`db-upgrade`, `lease-dump`, `lease-upload` and `stats-recount`. So the way
back is to restore your backup, which is why the backup comes first.

## Take a backup with your database's own image

`kea-tools` includes the `mysql` and `psql` clients that `kea-admin` needs,
but not the backup tools (`mysqldump`, `mariadb-dump`, `pg_dump`). Adding
them would make the image about 65 MB bigger for tools nothing inside it uses.

That catches people out, because running `mysqldump` through `kea-tools` can
leave you with an empty file and no obvious error. Use the database's own
image instead:

```bash
# MySQL / MariaDB
docker run --rm --network <net> mariadb:11 \
  mariadb-dump -h <host> -u <user> -p<password> <database> > kea-backup.sql

# PostgreSQL
docker run --rm --network <net> postgres:18 \
  pg_dump -h <host> -U <user> -d <database> > kea-backup.sql
```

Then check the backup actually has something in it:

```bash
test -s kea-backup.sql && grep -c 'CREATE TABLE' kea-backup.sql
```

A full Kea database has around 63 tables.

`kea-tools` *can* export just the leases, which is a handy extra, but it
doesn't replace the full backup, since it leaves out hosts, reservations,
options and statistics:

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin lease-dump mysql -h <host> -u <user> -p <password> -n <db> \
    -4 -o /tmp/leases4.csv
```

## Step by step

### 1. Stop everything that uses the database

That could be both `kea-dhcp4` and `kea-dhcp6`, and the second server of an
HA pair if you have one. Upgrading the schema underneath a running server
isn't supported.

```bash
docker stop kea4 kea6
```

### 2. Back up

As above, and check the file isn't empty.

### 3. Check which version you're on

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.0 \
  kea-admin db-version mysql -h <host> -u <user> -p <password> -n <db>
# -> 30.0
```

### 4. Upgrade, using the tools for the version you're moving to

The upgrade scripts live inside the image, so it's the 3.2 tools that know
how to get to 35.0:

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin db-upgrade mysql -h <host> -u <user> -p <password> -n <db>
```

On a real 30.0 → 35.0 upgrade, the output looks like this:

```
Processing /usr/share/kea/scripts/mysql/upgrade_030_to_031.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_031_to_032.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_032_to_033.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_033_to_034.sh file...
Processing /usr/share/kea/scripts/mysql/upgrade_034_to_035.sh file...
Schema version reported after initialization: 35.0
```

For PostgreSQL, use `pgsql` instead of `mysql`. The options are the same.

### 5. Check, then start the new version

```bash
docker run --rm --network <net> ghcr.io/dapalab/kea-tools:3.2 \
  kea-admin db-version mysql -h <host> -u <user> -p <password> -n <db>
# -> 35.0
```

Start `ghcr.io/dapalab/kea-dhcp4:3.2` and look for `DHCP4_STARTED` in the
log. If the schema were still wrong, Kea would have stopped before printing
that line.

## Keeping passwords off the command line

The examples above pass the password as an argument, which means it can show
up in `docker inspect`, `ps` and your shell history. For the servers
themselves, Kea can read it from a file instead:

```json
"lease-database": {
  "type": "mysql",
  "host": "db.example.net",
  "name": "kea",
  "user": "kea",
  "password-file": "/run/secrets/kea-db-password"
}
```

`kea-admin` still needs `-p`, so run it somewhere the command won't be saved,
for example not from a shell that keeps its history.

## Going from 3.2 back to 3.0

3.0 is the long-term support branch and is supported for about eleven months
longer than 3.2, so moving to it for stability is perfectly reasonable. In
database terms it's a *downgrade*, though, so it means restoring a 3.0 backup
rather than running a command. Plan it as a migration, not a quick rollback.
