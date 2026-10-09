#!/usr/bin/env bash
# Bring Forge's core from main into the Forge Web branch: merge, check, push only when green.
#
# Forge Web lives on its own branch until it is merged into main. The sync workflow
# (.github/workflows/forge-web-sync.yml) runs this after every push to main; it also works by
# hand from any clean checkout. Your own branch is never changed: the merge happens on a
# detached copy of what is pushed, and only a merge that passes Forge Web's checks is pushed.
#
# When someone pushes to the branch while the checks run, the push is refused and the sync starts
# over on top of their work (at most three rounds).
#
# Exit codes: 0 nothing new or merged and pushed, 2 the merge conflicts, 3 Forge Web's checks
# fail with the core changes, 4 the checkout has uncommitted changes, 5 the push failed.
#   SYNC_REMOTE  the remote (default: origin)
#   SYNC_MAIN    the core branch (default: main)
#   SYNC_BRANCH  the Forge Web branch (default: the branch checked out)
#   SYNC_GATE    the shell command that checks Forge Web (default: lint, types and tests)
set -euo pipefail

remote="${SYNC_REMOTE:-origin}"
main="${SYNC_MAIN:-main}"
cd "$(git rev-parse --show-toplevel)"
branch="${SYNC_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
gate="${SYNC_GATE:-cd forge-web && uv sync && uv run ruff check . && uv run mypy packages && uv run pytest -q}"

report() {  # the outcome for the workflow's next steps
  if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "result=$1" >>"$GITHUB_OUTPUT"; fi
}

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "This checkout has uncommitted changes: commit or stash them first." >&2
  report dirty
  exit 4
fi

start="$(git symbolic-ref --quiet --short HEAD || git rev-parse HEAD)"
trap 'git checkout --quiet "$start"' EXIT

for round in 1 2 3; do
  git fetch --quiet "$remote" "$main" "$branch"
  git checkout --quiet --detach "$remote/$branch"
  if git merge-base --is-ancestor "$remote/$main" HEAD; then
    echo "Forge Web already has everything from $main."
    report current
    exit 0
  fi
  if ! git merge --quiet --no-edit -m "Merge Forge's core from $main into Forge Web" "$remote/$main"; then
    git merge --abort
    echo "The core changes on $main conflict with Forge Web; nothing was pushed." >&2
    report conflict
    exit 2
  fi
  if ! bash -c "$gate"; then
    git reset --quiet --hard
    echo "Forge Web's checks fail with the core changes on $main; nothing was pushed." >&2
    report failed
    exit 3
  fi
  if git push --quiet "$remote" "HEAD:refs/heads/$branch"; then
    echo "Merged $main into $branch and pushed it."
    report pushed
    exit 0
  fi
  checked="$(git rev-parse HEAD^1)"
  git fetch --quiet "$remote" "$branch"
  if [ "$(git rev-parse "$remote/$branch")" = "$checked" ]; then
    break  # the branch did not move: the push failed for another reason
  fi
  echo "Someone pushed to $branch during round $round of the checks; starting over." >&2
done
echo "Could not push the merge to $branch; nothing was pushed." >&2
report push
exit 5
