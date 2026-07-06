
````txt
# Blockchain Schema Backup – Restore Instructions

This backup was created with `pg_dump` / `pg_dumpall` and contains **no data**, only:
- Roles / privileges (globals.sql)
- Schema objects for `blockchain` (tables, enums, sequences, indexes, triggers, functions, GRANTs)

## Files
- globals.sql              → global roles, grants, tablespaces
- schema_blockchain.sql    → full schema definition for `blockchain`
- MANIFEST.txt             → metadata (timestamp, DB, schema, etc.)

---

## Prerequisites
- PostgreSQL installed
- A target database named `postgres` (or another DB of your choice)
- User with superuser rights (same as `pgadmin` during backup)
- If using a password, set in environment:
  export PGPASSWORD="your_password"

---

## Restore Steps

### 1. Create the database (if not already present)
```bash
createdb -h localhost -U pgadmin postgres
````

### 2. Restore globals (roles, privileges)

```bash
psql -h localhost -U pgadmin -d postgres -f globals.sql
```

⚠️ Skip this step if restoring into an environment where roles are already managed separately.

### 3. Restore the blockchain schema

```bash
psql -h localhost -U pgadmin -d postgres -f schema_blockchain.sql
```

This recreates:

* Enums (`address_type`, `block_job_status`, etc.)
* Tables (`block_jobs`, `transactions`, `transaction_io`, `spends`, `watch_addresses`)
* Indexes, constraints, triggers
* Functions (e.g., `touch_updated_at`)
* Ownership and privileges

---

## Notes

* **No data is restored** – the schema will be empty.
* If restoring to a DB where roles differ from the source:

  * Use `--no-owner --no-privileges` during backup, or
  * Edit ownership statements inside `schema_blockchain.sql`.
* For full point-in-time recovery, pair with WAL archiving or logical backups.

---

## One-liner restore example

```bash
psql -h localhost -U pgadmin -d postgres -f globals.sql
psql -h localhost -U pgadmin -d postgres -f schema_blockchain.sql
```

```


