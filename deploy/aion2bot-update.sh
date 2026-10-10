#!/usr/bin/env bash
# Update the bot to the latest code on main and restart it. Safe to run repeatedly; it does
# nothing when there is nothing new. enable-updates.sh writes /etc/aion2bot-update.env.
#   sudo bash deploy/aion2bot-update.sh          # update now
# A fifteen-minute systemd timer (deploy/enable-updates.sh) runs this on its own.
set -euo pipefail
# shellcheck disable=SC1091
[ -f /etc/aion2bot-update.env ] && . /etc/aion2bot-update.env
SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONTAINER="${CONTAINER:-aion2-checklist}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"

# The checkout belongs to the account that installed the bot. Run Git as that account
# so its SSH configuration and deploy key are available to the updater.
GIT_OWNER="$(stat -c %U "$SRC")"
GIT_HOME="$(getent passwd "$GIT_OWNER" | cut -d: -f6)"
git_in_checkout() {
  if [ "$(id -u)" -eq 0 ] && [ "$GIT_OWNER" != root ]; then
    runuser -u "$GIT_OWNER" -- env HOME="$GIT_HOME" git -C "$SRC" "$@"
  else
    git -C "$SRC" "$@"
  fi
}

# systemd serializes this unit; the lock also covers direct manual invocation.
exec 9>/run/lock/aion2bot-update.lock
if ! flock -n 9; then
  echo "Another aion2bot update is already running."
  exit 0
fi
if [ "$(git_in_checkout branch --show-current)" != main ]; then
  echo "Automatic updater requires a main checkout; refusing to change branches." >&2
  exit 1
fi
# .env, data/ and data backups are ignored by Git; anything else that was edited is yours.
if [ -n "$(git_in_checkout status --porcelain --untracked-files=no)" ]; then
  echo "Checkout has local changes to tracked files; refusing to overwrite them." >&2
  exit 1
fi
before=$(git_in_checkout rev-parse HEAD)
git_in_checkout fetch --quiet origin main
candidate=$(git_in_checkout rev-parse origin/main)
STAMP="$SRC/data/.aion2bot-applied-revision"
applied=$(cat "$STAMP" 2>/dev/null || true)
if [ "$before" = "$candidate" ] && [ "$applied" = "$candidate" ]; then
  echo "aion2bot already installed and up to date ($candidate)"
  exit 0
fi
# Never deploy a push while its main CI is pending or failed.
# Public repositories work anonymously. Private repositories need a read-only
# Actions token in a protected file; never put the token in command arguments.
UPDATE_REPO="${UPDATE_REPO:-sam-t-anderson/Aion-2-fera-discordbot}"
UPDATE_GITHUB_TOKEN_FILE="${UPDATE_GITHUB_TOKEN_FILE:-/etc/aion2bot-github-token}"
if ! python3 "$SRC/deploy/check-update-ci.py" "$candidate" "$UPDATE_REPO" "$UPDATE_GITHUB_TOKEN_FILE"; then
  echo "Update deferred for $candidate; see the CI check above. The running bot was left in place."
  exit 0
fi

# Copy the data first (the database migrates itself on start), keeping the newest few copies.
if [ -d "$SRC/data" ]; then
  backup="$SRC/data.bak-auto-$(date +%Y%m%d-%H%M%S)"
  cp -a "$SRC/data" "$backup"
  echo "Data copied to $backup"
  find "$SRC" -maxdepth 1 -type d -name 'data.bak-auto-*' | sort | head -n "-$KEEP_BACKUPS" | while IFS= read -r old; do
    rm -rf -- "$old"
  done
fi

git_in_checkout merge --ff-only "$candidate"
after=$(git_in_checkout rev-parse HEAD)
echo "Installing verified bot revision $after (last applied: ${applied:-unknown})"
docker compose --project-directory "$SRC" up -d --build

# Docker's own health check passes once the bot is connected to Discord.
state=starting
for _ in $(seq 1 36); do
  state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER" 2>/dev/null || echo missing)
  if [ "$state" = healthy ]; then
    break
  fi
  sleep 5
done
if [ "$state" != healthy ]; then
  echo "The bot did not report healthy after the update (state: $state). Check: docker logs --tail 50 $CONTAINER" >&2
  exit 1
fi
# Mark applied only after the rebuild and health check succeed, so a failed install is
# retried even though Git already advanced.
printf '%s\n' "$after" > "$STAMP.tmp"
mv "$STAMP.tmp" "$STAMP"
echo "aion2bot updated $before -> $after and restarted"
