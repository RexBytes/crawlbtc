#!/usr/bin/env bash
set -euo pipefail

# === Config ===
DB="postgres"            # database name
USER="pgadmin"           # db user
HOST="localhost"         # host
SCHEMA="blockchain"      # schema to back up

# Optional: set PGPASSWORD in env or use ~/.pgpass
# export PGPASSWORD="your_password"

TS="$(date +%Y%m%d-%H%M%S)"
OUTDIR="./schema_backups/${TS}"
mkdir -p "$OUTDIR"

echo "➡️  Writing backups to: $OUTDIR"

# 1) Globals (roles, tablespaces, databases, default privs)
#    This includes GRANTs/REVOKEs at the global level.
pg_dumpall \
  -h "$HOST" -U "$USER" \
  --globals-only \
  > "${OUTDIR}/globals.sql"

# 2) Schema-only dump of the target schema (tables, types/enums, sequences, indexes,
#    constraints, triggers, functions, ownership, privileges) — NO DATA.
#    --clean/--if-exists makes the script re-runnable by dropping objects first.
pg_dump \
  -h "$HOST" -U "$USER" -d "$DB" \
  --schema-only \
  --schema="$SCHEMA" \
  --clean --if-exists \
  --quote-all-identifiers \
  -f "${OUTDIR}/schema_${SCHEMA}.sql"

# (Optional) Inventory file
{
  echo "Backup time: $(date -Iseconds)"
  echo "DB: $DB"
  echo "Host: $HOST"
  echo "User: $USER"
  echo "Schema: $SCHEMA"
  echo "Files:"
  echo "  - globals.sql"
  echo "  - schema_${SCHEMA}.sql"
} > "${OUTDIR}/MANIFEST.txt"

echo "✅ Done."
echo "Files:"
echo "  ${OUTDIR}/globals.sql"
echo "  ${OUTDIR}/schema_${SCHEMA}.sql"

