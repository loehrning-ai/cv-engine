#!/usr/bin/env bash
# Replay every Supabase migration onto a disposable PostgreSQL cluster and
# execute the behavioural probes in tools/db/migration_probes.sql.
#
# Why this exists: the Python suite asserts the database contract by matching
# text inside the .sql files. That proves a migration SAYS the right thing, not
# that PostgreSQL DOES it. This script is the executable half — it caught both
# an over-broad delete guard and an inert LIKE-escape sanitizer that every
# text-level assertion happily passed.
#
# Requires a local PostgreSQL toolchain (initdb, pg_ctl, psql). Nothing is read
# from or written to any hosted project; the cluster is created in a temporary
# directory and destroyed on exit, including on failure.
#
#   ./tools/db/replay_migrations.sh            # default port 55432
#   PGPROBE_PORT=55444 ./tools/db/replay_migrations.sh
#
# Exit codes: 0 all migrations applied and all probes passed; 1 otherwise;
# 127 when the PostgreSQL toolchain is unavailable.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${PGPROBE_PORT:-55432}"
DB=cv_migration_replay

for tool in initdb pg_ctl psql; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "SKIP: $tool not found — install PostgreSQL to run the migration probes." >&2
    exit 127
  }
done

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: port $PORT is already in use. Set PGPROBE_PORT to a free port." >&2
  exit 1
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/cv-pgreplay.XXXXXX")"
PGDATA="$WORKDIR/pgdata"

cleanup() {
  pg_ctl -D "$PGDATA" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "== initdb ($(initdb --version)) =="
initdb -D "$PGDATA" -U postgres --auth=trust >"$WORKDIR/initdb.log" 2>&1 || {
  echo "ERROR: initdb failed" >&2; tail -20 "$WORKDIR/initdb.log" >&2; exit 1
}

# Socket paths are capped at ~103 bytes and a temp dir can exceed that, so bind
# loopback TCP instead of a unix socket.
pg_ctl -D "$PGDATA" -l "$WORKDIR/pg.log" \
  -o "-p $PORT -k /tmp -c listen_addresses=127.0.0.1" -w start >/dev/null 2>&1 || {
  echo "ERROR: could not start PostgreSQL" >&2; tail -20 "$WORKDIR/pg.log" >&2; exit 1
}

psql_run() { psql -h 127.0.0.1 -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q "$@"; }

psql_run -d postgres -c "create database $DB;" >/dev/null || exit 1

# Minimal Supabase shim: the roles the grants target, plus auth.uid()/auth.users
# which the RLS policies and owner defaults reference.
echo "== supabase shim =="
psql_run -d "$DB" >/dev/null <<'SQL' || { echo "ERROR: shim failed" >&2; exit 1; }
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
grant anon, authenticated, service_role to postgres;
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key, email text);
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
grant usage on schema auth to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
SQL

echo "== replaying migrations =="
applied=0
for migration in "$REPO"/supabase/migrations/*.sql; do
  if psql_run -d "$DB" -f "$migration" >"$WORKDIR/mig.log" 2>&1; then
    applied=$((applied + 1))
    printf '  ok   %s\n' "$(basename "$migration")"
  else
    printf '  FAIL %s\n' "$(basename "$migration")"
    sed 's/^/       /' "$WORKDIR/mig.log" | tail -10
    echo "ERROR: migration replay failed" >&2
    exit 1
  fi
done
echo "  $applied migrations applied"

echo "== behavioural probes =="
if psql_run -d "$DB" -f "$REPO/tools/db/migration_probes.sql" 2>&1 | sed 's/^/  /'; then
  echo "OK: migrations replay cleanly and every probe passed."
else
  echo "ERROR: migration probes failed" >&2
  exit 1
fi
