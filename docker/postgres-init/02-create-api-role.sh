#!/bin/sh
set -e

# Creates a dedicated Postgres login role for the API so a future migration/entrypoint
# change can stop the API connecting as the initdb bootstrap superuser (issue #330) --
# POSTGRES_USER always becomes a superuser on the official postgres image, and
# superusers unconditionally bypass Row-Level Security regardless of ENABLE/FORCE,
# which makes migrations 0030/0031's RLS policies currently enforce nothing in
# production. Unlike the worker (see 01-create-worker-role.sh), this role does NOT get
# BYPASSRLS -- the whole point is for the API to actually be subject to RLS.
#
# Runs via docker-entrypoint-initdb.d, so it only executes once, on a completely fresh
# data volume. Existing deployments (pgdata already initialized) must create this role
# manually -- see docs/self-hosting.md.
#
# No-op if API_DB_PASSWORD isn't set, so deployments that haven't opted in keep today's
# shared-credential (superuser) behavior untouched. This migration only grants table
# privileges to the role once it exists -- see migration 0032 -- it does not yet make
# the runtime API connection use it (that cutover is a separate, deliberately later
# change; see issue #330).
if [ -z "$API_DB_PASSWORD" ]; then
  exit 0
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v api_password="$API_DB_PASSWORD" -v db_name="$POSTGRES_DB" <<-'EOSQL'
-- psql's :'var' substitution doesn't reach inside a dollar-quoted string, so the DO
-- block is built as text via format()/%L (which also handles escaping the password
-- safely) and executed with \gexec, rather than interpolating :'api_password'
-- directly inside $do$...$do$.
SELECT format($fmt$
DO $do$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
    CREATE ROLE clevis_api WITH LOGIN PASSWORD %L;
  END IF;
END
$do$;
$fmt$, :'api_password') \gexec

GRANT CONNECT ON DATABASE :"db_name" TO clevis_api;
EOSQL
