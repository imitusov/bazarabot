#!/usr/bin/env bash
# Pull-based deployer. Runs on the VPS from a systemd timer.
#
# Pull rather than push, deliberately: no SSH key from GitHub into the machine
# holding the money, and the VPS decides WHEN it is safe to restart. A runner
# cannot know whether an order is in flight; this script can.
#
# Refuses to deploy unless every preconditions holds, verifies the new
# container actually came up, and rolls back to the previous image if it did not.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/opt/zarabot/app}"
DATA_DIR="${DATA_DIR:-/opt/zarabot/data}"
IMAGE="${IMAGE:-ghcr.io/imitusov/bazarabot}"
TAG="${TAG:-candidate}"
DB="${DB:-$DATA_DIR/zarabot.db}"
STATE="${STATE:-$DATA_DIR/.deployed-digest}"
WINDOW_START="${WINDOW_START:-2}"     # MSK hour, inclusive
WINDOW_END="${WINDOW_END:-5}"         # MSK hour, exclusive
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"

log() { printf '%s %s\n' "$(date -Is)" "$*"; }
die() { log "REFUSED: $*"; exit 0; }          # exit 0: not an error, just not now

cd "$REPO_DIR"

# --- preconditions -----------------------------------------------------------

hour=$(TZ=Europe/Moscow date +%-H)
if [ "$hour" -lt "$WINDOW_START" ] || [ "$hour" -ge "$WINDOW_END" ]; then
  die "outside the deploy window (${WINDOW_START}:00-${WINDOW_END}:00 MSK, now ${hour}:00)"
fi

# An order in SUBMITTING means the broker's answer is unknown. Restarting is
# recoverable by design, but there is no reason to spend that margin on a
# routine update - wait for the next window instead.
if [ -f "$DB" ]; then
  inflight=$(sqlite3 "$DB" "SELECT COUNT(*) FROM orders WHERE status IN ('SUBMITTING','SUBMITTED');" 2>/dev/null || echo 0)
  [ "$inflight" = "0" ] || die "$inflight order(s) in flight"
fi

docker pull -q "$IMAGE:$TAG" >/dev/null
new_digest=$(docker image inspect "$IMAGE:$TAG" --format '{{index .RepoDigests 0}}' 2>/dev/null || echo "")
old_digest=$(cat "$STATE" 2>/dev/null || echo "")
[ -n "$new_digest" ] || die "could not resolve the image digest"
[ "$new_digest" != "$old_digest" ] || { log "already on $new_digest"; exit 0; }

# --- keep a rollback target --------------------------------------------------

rollback=$(docker inspect --format '{{.Image}}' zarabot 2>/dev/null || echo "")
log "deploying $new_digest (rollback target: ${rollback:-none})"

# --- deploy ------------------------------------------------------------------

IMAGE_REF="$new_digest" docker compose -f docker-compose.deploy.yml up -d --no-build

# --- verify ------------------------------------------------------------------

deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
healthy=false
while [ "$(date +%s)" -lt "$deadline" ]; do
  if docker compose -f docker-compose.deploy.yml logs --since 5m 2>/dev/null | grep -q '"event": *"startup_ok"'; then
    healthy=true; break
  fi
  if ! docker compose -f docker-compose.deploy.yml ps --status running --quiet | grep -q .; then
    break                                    # container exited: fail fast
  fi
  sleep 5
done

if [ "$healthy" = true ]; then
  echo "$new_digest" > "$STATE"
  log "OK: startup_ok observed"
  exit 0
fi

# --- rollback ----------------------------------------------------------------
# A deploy that produces no startup_ok has failed, whatever `ps` says - the spec
# says the same thing about the Telegram alert.

log "FAILED: no startup_ok within ${HEALTH_TIMEOUT}s"
docker compose -f docker-compose.deploy.yml logs --tail 40 || true
if [ -n "$rollback" ]; then
  log "rolling back to $rollback"
  IMAGE_REF="$rollback" docker compose -f docker-compose.deploy.yml up -d --no-build
else
  log "no rollback target; leaving the stack as-is for inspection"
fi
exit 1
