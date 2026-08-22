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
GRANT SELECT, INSERT, UPDATE, DELETE ON users, orgs, org_memberships, tenants, memberships, invitations, github_installations, saved_tokens, audit_logs, scan_results, jobs, app_config, webhook_deliveries TO clevis_api;
GRANT USAGE, SELECT ON users_id_seq, orgs_id_seq, org_memberships_id_seq, tenants_id_seq, memberships_id_seq, invitations_id_seq, github_installations_id_seq, saved_tokens_id_seq, audit_logs_id_seq, scan_results_id_seq, jobs_id_seq, webhook_deliveries_id_seq TO clevis_api;

-- repo_events (migration 0036, issue #191/S4 PR 1): the API doesn't write this table
-- itself (only apps/worker's consumer does; S6 will add API reads later), but CI runs
-- the whole pytest suite -- apps/worker's tests included -- under clevis_api (there's
-- no clevis_worker CI provisioning), so the consumer's own tests need this grant to
-- pass in CI. Same reasoning as clevis_worker's own grant in migration 0036. Guarded
-- by existence, unlike the unconditional grants above -- this script isn't only run
-- once per deployment (its own comment above documents it as safe to re-run for
-- password rotation too), and a re-run against a deployment that hasn't applied
-- migration 0036 yet would otherwise fail on "relation does not exist" and roll back
-- every grant in this transaction, including the ones that were fine (CodeRabbit
-- finding on PR #340).
DO $do$
BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'repo_events') THEN
    GRANT SELECT, INSERT ON repo_events TO clevis_api;
    GRANT USAGE, SELECT ON repo_events_id_seq TO clevis_api;
  END IF;
END
$do$;

-- repo_event_daily_counts (migration 0037, issue #191/S4 PR 2): same reasoning as
-- repo_events immediately above -- CI runs apps/worker's tests as clevis_api, and this
-- table has no sequence (composite PK), so only the table grant is needed.
DO $do$
BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'repo_event_daily_counts') THEN
    GRANT SELECT, INSERT, UPDATE ON repo_event_daily_counts TO clevis_api;
  END IF;
END
$do$;

-- activity_sync_cursors (migration 0038, issue #192/S5 PR 2): same reasoning as
-- repo_event_daily_counts immediately above -- CI runs apps/worker's tests as clevis_api, and this
-- table has no sequence (tenant_id is the PK), so only the table grant is needed.
DO $do$
BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'activity_sync_cursors') THEN
    GRANT SELECT, INSERT, UPDATE ON activity_sync_cursors TO clevis_api;
  END IF;
END
$do$;

-- security_alerts (migration 0039, post-S6 PR 2): same reasoning as activity_sync_cursors
-- immediately above -- CI runs apps/worker's tests as clevis_api, and this table upserts
-- (not just inserts), so UPDATE is needed alongside SELECT/INSERT; it does have a
-- surrogate id sequence, unlike the two tables above.
DO $do$
BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'security_alerts') THEN
    GRANT SELECT, INSERT, UPDATE ON security_alerts TO clevis_api;
    GRANT USAGE, SELECT ON security_alerts_id_seq TO clevis_api;
  END IF;
END
$do$;

-- org_members + repo_collaborators (migration 0040, Collaborators PR 1): same reasoning as
-- security_alerts immediately above -- both upsert AND delete (a row is removed on
-- member_removed/removed, not soft-marked), so DELETE is needed alongside SELECT/INSERT/UPDATE;
-- both have a surrogate id sequence.
DO $do$
BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'org_members') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON org_members TO clevis_api;
    GRANT USAGE, SELECT ON org_members_id_seq TO clevis_api;
  END IF;
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'repo_collaborators') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON repo_collaborators TO clevis_api;
    GRANT USAGE, SELECT ON repo_collaborators_id_seq TO clevis_api;
  END IF;
END
$do$;

-- resolve_installation_tenant_id() (migration 0035) REVOKEs its default PUBLIC EXECUTE
-- and re-GRANTs it only to clevis_api -- but that migration's own GRANT is itself
-- conditional on clevis_api already existing, which isn't true the first time this
-- script runs on a deployment that's adopting the role. Guarded by existence so this
-- script still works against a deployment where migration 0035 hasn't run yet.
DO $do$
BEGIN
  IF EXISTS (SELECT FROM pg_proc WHERE proname = 'resolve_installation_tenant_id') THEN
    GRANT EXECUTE ON FUNCTION resolve_installation_tenant_id(integer) TO clevis_api;
  END IF;
END
$do$;

COMMIT;
EOSQL
