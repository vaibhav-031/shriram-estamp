#!/usr/bin/env bash
# Throwaway Postgres in Docker for the DB-backed tests.
#
# The end-to-end tests read ESTAMP_TEST_DB_URL and create/drop their own schema
# (estamp_test) – they never touch anything else (see tests/test_all.py). This
# script starts a disposable Postgres, points the tests at it, runs them, and
# removes the container on exit. Lets the suite run anywhere Docker is available
# without a local Postgres.app.
#
#   scripts/with-test-db.sh               # run the suite 5× (the CLAUDE.md gate)
#   scripts/with-test-db.sh <cmd...>      # run <cmd> with ESTAMP_TEST_DB_URL set
#
# For parallel runs (e.g. several worktree engineers at once) give each a unique
# container + port: TEST_DB_NAME=estamp-eng1 TEST_DB_PORT=55441 scripts/with-test-db.sh ...
set -euo pipefail

PORT="${TEST_DB_PORT:-55432}"
NAME="${TEST_DB_NAME:-estamp-test-db}"   # override both for parallel runs (e.g. worktree engineers)
IMAGE="postgres:16-alpine"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup  # clear any leftover from a crashed previous run
# trust auth = passwordless, so bind to loopback only – never expose an
# unauthenticated Postgres on the LAN.
docker run -d --name "$NAME" -e POSTGRES_HOST_AUTH_METHOD=trust \
	-p "127.0.0.1:$PORT:5432" "$IMAGE" >/dev/null

# The image boots a temp server on the unix socket to initialise, then the real
# one on TCP. pg_isready over TCP (-h 127.0.0.1) only succeeds once the real
# server accepts connections – a direct readiness signal with no race.
echo "starting postgres on :$PORT ..."
ready=
for _ in $(seq 1 60); do
	if docker exec "$NAME" pg_isready -h 127.0.0.1 -U postgres -q; then ready=1; break; fi
	sleep 0.5
done
if [ -z "$ready" ]; then
	echo "postgres never came up; last logs:" >&2
	docker logs --tail 20 "$NAME" >&2
	exit 1
fi

export ESTAMP_TEST_DB_URL="postgresql://postgres@localhost:$PORT/postgres"

if [ "$#" -eq 0 ]; then
	for i in 1 2 3 4 5; do
		echo "--- run $i/5 ---"
		uv run pytest -q tests/ || exit 1
	done
else
	"$@"
fi
