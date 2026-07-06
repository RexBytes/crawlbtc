#!/usr/bin/env bash
set -euo pipefail

DB="postgres"
USER="pgadmin"
HOST="localhost"

# If needed:
# export PGPASSWORD="your_password"

echo "Restoring globals (roles/privileges)…"
psql -h "$HOST" -U "$USER" -d postgres -f globals.sql

echo "Restoring blockchain schema into $DB…"
psql -h "$HOST" -U "$USER" -d "$DB" -f schema_blockchain.sql

echo "✅ Restore complete."

