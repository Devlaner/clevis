#!/bin/sh
set -e

if [ -z "$DB_NAME" ] || [ -z "$JOB_SECRET_KEY" ]; then
  echo "ERROR: DB_NAME and JOB_SECRET_KEY must be set" >&2
  exit 1
fi

# Prefer a dedicated worker DB role (issue #190) over the credential shared with the
# API -- see docker/postgres-init/01-create-worker-role.sh for how it's provisioned.
if [ -n "$WORKER_DB_PASSWORD" ]; then
  export DATABASE_URL="postgresql+psycopg://clevis_worker:${WORKER_DB_PASSWORD}@db:5432/${DB_NAME}"
elif [ -n "$DB_USER" ] && [ -n "$DB_PASSWORD" ]; then
  export DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@db:5432/${DB_NAME}"
else
  echo "ERROR: either WORKER_DB_PASSWORD or (DB_USER and DB_PASSWORD) must be set" >&2
  exit 1
fi

exec python src/worker.py
