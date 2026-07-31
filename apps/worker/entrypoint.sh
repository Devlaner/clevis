#!/bin/sh
set -e

if [ -z "$DB_NAME" ] || [ -z "$JOB_SECRET_KEY" ]; then
  echo "ERROR: DB_NAME and JOB_SECRET_KEY must be set" >&2
  exit 1
fi

# Percent-encodes a value so URI delimiters in it (@, :, /, %, #, ...) can't be
# misparsed as part of the connection URL's structure (e.g. an "@" in a password
# would otherwise be read as the userinfo/host separator).
_urlenc() {
  python -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote_plus(sys.argv[1]))' "$1"
}

_db_name_enc=$(_urlenc "$DB_NAME")

# Prefer a dedicated worker DB role (issue #190) over the credential shared with the
# API -- see docker/postgres-init/01-create-worker-role.sh for how it's provisioned.
if [ -n "$WORKER_DB_PASSWORD" ]; then
  export DATABASE_URL="postgresql+psycopg://clevis_worker:$(_urlenc "$WORKER_DB_PASSWORD")@db:5432/${_db_name_enc}"
elif [ -n "$DB_USER" ] && [ -n "$DB_PASSWORD" ]; then
  export DATABASE_URL="postgresql+psycopg://$(_urlenc "$DB_USER"):$(_urlenc "$DB_PASSWORD")@db:5432/${_db_name_enc}"
else
  echo "ERROR: either WORKER_DB_PASSWORD or (DB_USER and DB_PASSWORD) must be set" >&2
  exit 1
fi

exec python src/worker.py
