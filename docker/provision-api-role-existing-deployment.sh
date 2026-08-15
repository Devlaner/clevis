#!/bin/sh
set -e

# Manual provisioning for the clevis_api Postgres role on an EXISTING deployment (a db
# volume that's already initialized, so docker-entrypoint-initdb.d/02-create-api-role.sh
# won't run automatically -- see docs/self-hosting.md).
#
# Deliberately NOT placed under docker/postgres-init/: that whole directory auto-runs on
# every fresh volume via docker-entrypoint-initdb.d, at a point before Alembic has
# created any tables yet -- the GRANT ... ON users, orgs, ... statements below would
# fail with "relation does not exist" if this ran automatically on a fresh volume. This
# script is for the existing-deployment case only, where those tables already exist,
# and is meant to be copied into the db container and run by hand.
#
# Bundles role creation and every grant (schema, table, sequence -- matching migration
# 0032's grant list exactly) into ONE psql invocation wrapped in a single transaction,
# so a failure partway through leaves the role either fully provisioned or not created
# at all -- never stuck with CONNECT but no table access (CodeRabbit finding on #332:
# an earlier version of docs/self-hosting.md ran role creation and grants as two
# separate psql sessions, which isn't atomic).
if [ -z "$API_DB_PASSWORD" ]; then
  echo "ERROR: API_DB_PASSWORD must be set in the db container's environment" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v api_password="$API_DB_PASSWORD" -v db_name="$POSTGRES_DB" <<-'EOSQL'
BEGIN;

-- psql's :'var' substitution doesn't reach inside a dollar-quoted string, so the DO
-- block is built as text via format()/%L (which also handles escaping the password
-- safely) and executed with \gexec, rather than interpolating :'api_password'
-- directly inside $do$...$do$. Mirrors docker/postgres-init/02-create-api-role.sh.
SELECT format($fmt$
DO $do$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
    CREATE ROLE clevis_api WITH LOGIN PASSWORD %L;
  END IF;
END
$do$;
$fmt$, :'api_password') \gexec

-- Runs unconditionally (not just on first creation) so re-running this script always
-- converges the role to the current API_DB_PASSWORD and to the least-privilege
-- attributes below -- covering both a rotated password and a role that was somehow
-- created with broader attributes than intended (e.g. by hand, before this script
-- existed). NOBYPASSRLS in particular matters: the whole point of this role (issue
-- #330) is to actually be subject to Row-Level Security, unlike DB_USER.
SELECT format($fmt$
ALTER ROLE clevis_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L;
$fmt$, :'api_password') \gexec

GRANT CONNECT ON DATABASE :"db_name" TO clevis_api;
GRANT USAGE ON SCHEMA public TO clevis_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, orgs, org_memberships, tenants, memberships, invitations, github_installations, saved_tokens, audit_logs, scan_results, jobs, app_config TO clevis_api;
GRANT USAGE, SELECT ON users_id_seq, orgs_id_seq, org_memberships_id_seq, tenants_id_seq, memberships_id_seq, invitations_id_seq, github_installations_id_seq, saved_tokens_id_seq, audit_logs_id_seq, scan_results_id_seq, jobs_id_seq TO clevis_api;

COMMIT;
EOSQL
