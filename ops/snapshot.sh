#!/usr/bin/env bash
# Push a snapshot of every local worktree (committed or not) to origin as wip/<name>, without touching
# the worktree's own index, branch or files. Safe to run while agents are editing.
cd /home/user/paperdesk || exit 1
git worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
  name=$(basename "$wt"); [ "$wt" = /home/user/paperdesk ] && name=lead-main
  ( cd "$wt" 2>/dev/null || exit 0
    idx=$(mktemp); cp "$(git rev-parse --git-path index)" "$idx" 2>/dev/null
    export GIT_INDEX_FILE="$idx"
    git add -A >/dev/null 2>&1
    tree=$(git write-tree 2>/dev/null) || { rm -f "$idx"; exit 0; }
    parent=$(git rev-parse HEAD)
    c=$(git -c user.name=PaperDesk -c user.email=noreply@example.com commit-tree "$tree" -p "$parent" -m "WIP snapshot of worktree $name at $(date -u +%Y-%m-%dT%H:%MZ) (branch $(git rev-parse --abbrev-ref HEAD))")
    unset GIT_INDEX_FILE; rm -f "$idx"
    for i in 1 2 3; do git push -q -f origin "$c:refs/heads/wip/$name" 2>/dev/null && { echo "ok $name $c"; break; }; sleep 6; done
  )
done
