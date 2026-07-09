#!/usr/bin/env bash
# Publish a snapshot of the current HEAD tree to the public repository.
#
# The public repo carries release snapshots only (no development history).
# This script: exports the committed tree (git archive — untracked and
# gitignored files never leak), runs safety gates, then commits the snapshot
# to the public repo and pushes.
#
# Usage: scripts/release-public.sh [public-repo-url]
set -euo pipefail

PUBLIC_URL="${1:-git@github.com:meriler/claudebot.git}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is dirty — commit or stash first." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
EXPORT="$WORK/export"
mkdir "$EXPORT"
git archive HEAD | tar -x -C "$EXPORT"

# --- Safety gates -----------------------------------------------------------
# 1. Paths that must never appear in a public snapshot: private working docs,
#    task specs, legacy setup notes, per-instance deployment/user guides.
FORBIDDEN=(.docs .tasks SETUP.md)
for p in "${FORBIDDEN[@]}"; do
  if [ -e "$EXPORT/$p" ]; then
    echo "ERROR: forbidden path in export: $p" >&2
    exit 1
  fi
done
if find "$EXPORT" -name 'AUDIT-*' -o -name '*.bak-*' \
    -o -path '*/docs/*-deployment.md' -o -path '*/docs/how-to-*' | grep -q .; then
  echo "ERROR: private/audit/backup files in export." >&2
  exit 1
fi

# 2. Secret patterns (telegram bot tokens, common API keys).
if grep -rInE '[0-9]{8,10}:AA[A-Za-z0-9_-]{33}|sk-(ant|proj|or)-|ghp_[A-Za-z0-9]{36}|AKIA[0-9A-Z]{16}' "$EXPORT" >&2; then
  echo "ERROR: secret-looking strings in export (see matches above)." >&2
  exit 1
fi

# 3. gitleaks over the export, if installed (belt and suspenders).
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-git --source "$EXPORT"
fi

# --- Publish ----------------------------------------------------------------
PUB="$WORK/public"
git clone --depth 1 "$PUBLIC_URL" "$PUB" 2>/dev/null || {
  # Empty repo (first release) — clone fails on some git versions; init instead.
  git init -q -b main "$PUB"
  git -C "$PUB" remote add origin "$PUBLIC_URL"
}

rsync -a --delete --exclude '.git' "$EXPORT"/ "$PUB"/
SRC_COMMIT="$(git rev-parse --short HEAD)"
git -C "$PUB" add -A
if git -C "$PUB" diff --cached --quiet; then
  echo "Nothing to release: public repo already matches HEAD tree."
  exit 0
fi
git -C "$PUB" commit -q -m "Release $(date +%Y-%m-%d) (source ${SRC_COMMIT})"
git -C "$PUB" push origin main
echo "Published $(date +%Y-%m-%d) snapshot (source ${SRC_COMMIT}) to ${PUBLIC_URL}"
