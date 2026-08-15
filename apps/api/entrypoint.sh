#!/bin/sh
set -e

if [ -z "$DB_USER" ] || [ -z "$DB_NAME" ] || [ -z "$DB_PASSWORD" ] || [ -z "$JOB_SECRET_KEY" ] || [ -z "$AUTH_SECRET" ] || [ -z "$REDIS_PASSWORD" ]; then
  echo "ERROR: DB_USER, DB_NAME, DB_PASSWORD, JOB_SECRET_KEY, AUTH_SECRET, and REDIS_PASSWORD must all be set" >&2
  exit 1
fi

# Percent-encodes a value so URI delimiters in it (@, :, /, %, #, ...) can't be
# misparsed as part of the connection URL's structure (e.g. an "@" in a password
# would otherwise be read as the userinfo/host separator).
_urlenc() {
  python -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

_db_user_enc=$(_urlenc "$DB_USER")
_db_password_enc=$(_urlenc "$DB_PASSWORD")
_db_name_enc=$(_urlenc "$DB_NAME")

# Migrations always run under the DB_USER superuser credential -- it owns the tables
# (see migration 0030's ENABLE-without-FORCE reasoning: table owners bypass RLS unless
# FORCE is set) and needs DDL/GRANT privilege the runtime clevis_api role (issue #330)
# intentionally does not have.
export DATABASE_URL="postgresql+psycopg://${_db_user_enc}:${_db_password_enc}@db:5432/${_db_name_enc}"
export AUTH_SECRET
python -m alembic upgrade head

# Issue #330: the runtime app connects as a dedicated non-superuser clevis_api role
# when API_DB_PASSWORD is set, so Row-Level Security (migrations 0030/0031) actually
# applies to it for the first time -- DB_USER is the initdb bootstrap superuser and
# unconditionally bypasses RLS regardless of ENABLE/FORCE. Falls back to sharing
# DB_USER/DB_PASSWORD (today's behavior) when unset, same fallback shape as the
# worker's own entrypoint.
if [ -n "$API_DB_PASSWORD" ]; then
  export DATABASE_URL="postgresql+psycopg://clevis_api:$(_urlenc "$API_DB_PASSWORD")@db:5432/${_db_name_enc}"
fi

# Issue #191/S3: webhook ingestion queue. Built here (not left as a directly-set
# REDIS_URL) so the raw password only ever needs to be set once, the same shape as
# DB_USER/DB_PASSWORD/DB_NAME above -- REDIS_URL itself stays a "local dev outside
# Docker only" var (see .env.example), same as DATABASE_URL.
export REDIS_URL="redis://:$(_urlenc "$REDIS_PASSWORD")@redis:6379/0"

# --proxy-headers/--forwarded-allow-ips: trust X-Forwarded-Proto from Traefik so
# request.url_for() (used to build the GitHub OAuth callback/redirect_uri) reports the
# original https scheme instead of the http uvicorn actually receives behind the proxy.
# Without this, GitHub rejects the OAuth flow with "redirect_uri is not associated with
# this application" since the registered callback is https but the sent one was http.
# '*' is safe here — Traefik and the API only ever talk over the internal Docker network,
# never a public one.
exec uvicorn src.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'
