#!/usr/bin/env bash
# One-command bring-up for RFP Shred. Idempotent: safe to re-run.
#   ./up.sh              -> start the stack, migrate, bootstrap staff
#   ./up.sh --rebuild    -> also rebuild the image first
#   ./up.sh --reset      -> DESTROY the database volume and start clean
set -euo pipefail
cd "$(dirname "$0")"

EMAIL="${STAFF_EMAIL:-founder@example.com}"   # set STAFF_EMAIL to your operator address
C=(docker compose -f deploy/compose.yml)

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [[ "${1:-}" == "--reset" ]]; then
  say "Tearing down stack + database volume"
  "${C[@]}" down -v
fi

if [[ "${1:-}" == "--rebuild" || "${1:-}" == "--reset" ]]; then
  say "Rebuilding images"
  "${C[@]}" build          # all services: api, worker and sweep each build their own
fi

# Compose only builds when an image is ABSENT — it does not notice a changed
# Dockerfile. Prove the entry points actually exec, and rebuild if they don't.
say "Verifying image entry points"
if "${C[@]}" run --rm --no-deps -T api \
     sh -c 'alembic --version >/dev/null 2>&1 && uvicorn --version >/dev/null 2>&1'; then
  echo "alembic + uvicorn exec cleanly"
else
  say "Entry points broken or image stale — rebuilding"
  "${C[@]}" build
  "${C[@]}" run --rm --no-deps -T api \
    sh -c 'alembic --version && uvicorn --version'
fi

say "Starting Postgres (waiting for healthy)"
"${C[@]}" up -d --wait postgres

# THE STEP THAT WAS MISSING: no tables exist until this runs.
say "Applying database migrations (alembic upgrade head)"
"${C[@]}" run --rm api alembic upgrade head

say "Verifying schema"
"${C[@]}" exec -T postgres psql -U rfp -d rfp -c '\dt' | tail -n +1

say "Starting api + worker + sweep"
"${C[@]}" up -d api worker sweep

say "Waiting for /healthz"
for i in $(seq 1 40); do
  if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then
    echo "healthy after ${i}s"; break
  fi
  if [[ $i -eq 40 ]]; then
    echo "api never became healthy — last 60 log lines:" >&2
    "${C[@]}" logs --tail 60 api >&2
    exit 1
  fi
  sleep 1
done

say "Loading mock corpus fixtures (optional)"
"${C[@]}" run --rm api python -m scripts.download_corpus --mock-corpus || \
  echo "(corpus fixtures unavailable — the UI still runs)"

say "Bootstrapping staff account"
"${C[@]}" run --rm api python -m scripts.create_staff --email "$EMAIL"

cat <<EOF

------------------------------------------------------------
  UI is up:  http://localhost:8080/
  Ops console: http://localhost:8080/ops   (staff login above)
  Logs:  docker compose -f deploy/compose.yml logs -f api worker
  Stop:  docker compose -f deploy/compose.yml down
------------------------------------------------------------
EOF
