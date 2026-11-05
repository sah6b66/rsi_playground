"""Server-side pre-receive hook for the bare GitStore repo (DD-09 §5.2).

The hook enforces, at the repository boundary:
1. optimizer-identity pushes cannot touch ``judge/`` or ``prompts/``;
2. baseline tags (``suts/*/baseline/*``) and freeze tags
   (``evalsets/*/freeze/*``) can be created but never deleted or modified;
3. optimizer-identity pushes cannot write any ``evalsets/<id>/**`` path
   for which a freeze tag already exists.
"""

from __future__ import annotations

from pathlib import Path

PRE_RECEIVE_SCRIPT = """#!/bin/bash
# RSI Playground pre-receive hook
ZERO=0000000000000000000000000000000000000000
while read oldrev newrev refname; do
    # Rule 2 (checked BEFORE the deletion skip): protected tags can never
    # be deleted (newrev=0) or modified (oldrev!=0). Creation is allowed.
    if [[ "$refname" == refs/tags/suts/*/baseline/* ]] || [[ "$refname" == refs/tags/evalsets/*/freeze/* ]]; then
        if [ "$newrev" = "$ZERO" ]; then
            echo "ERROR: Cannot delete protected tag: $refname" >&2
            exit 1
        fi
        if [ "$oldrev" != "$ZERO" ]; then
            echo "ERROR: Cannot modify protected tag: $refname" >&2
            exit 1
        fi
    fi

    # Skip branch deletions (non-protected refs only reach here)
    [ "$newrev" = "$ZERO" ] && continue

    # Check commit author for restricted paths
    author=$(git log --format='%an' -1 "$newrev" 2>/dev/null || echo "")
    if [[ "$author" == *"optimizer"* ]] || [[ "$author" == *"rsi-optimizer"* ]]; then
        # Check if any changed file is in restricted paths
        changed_files=$(git diff --name-only "$oldrev" "$newrev" 2>/dev/null || git ls-tree --name-only -r "$newrev")
        if echo "$changed_files" | grep -qE "^(judge/|prompts/)"; then
            echo "ERROR: optimizer identity cannot modify judge/ or prompts/" >&2
            exit 1
        fi
        # Rule 3: an evalset that already has a freeze tag is fully frozen —
        # reject any optimizer write under evalsets/<that id>/** (not just
        # the freeze tag path itself).
        frozen_ids=$(git tag -l 'evalsets/*/freeze/*' | sed -E 's#^evalsets/([^/]+)/freeze/.*$#\\1#' | sort -u)
        if [ -n "$frozen_ids" ]; then
            while IFS= read -r f; do
                [ -z "$f" ] && continue
                case "$f" in
                    evalsets/*)
                        es_id=$(printf '%s\\n' "$f" | sed -E 's#^evalsets/([^/]+)/.*$#\\1#')
                        if printf '%s\\n' "$frozen_ids" | grep -qx "$es_id"; then
                            echo "ERROR: optimizer identity cannot modify frozen evalsets/ (evalset $es_id is frozen)" >&2
                            exit 1
                        fi
                        ;;
                esac
            done <<EOF
$(printf '%s\\n' "$changed_files")
EOF
        fi
    fi
done
exit 0
"""


def install_hooks(repo_path: Path) -> Path:
    """Write a pre-receive hook script to the bare repo's hooks/ directory.

    The script rejects:
    1. optimizer-identity pushes to judge/**, prompts/**
    2. Any push that deletes or modifies baseline or freeze tags
    3. optimizer-identity pushes to any frozen evalsets/<id>/**

    Returns the path of the installed hook.
    """
    hooks_dir = Path(repo_path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-receive"
    hook_path.write_text(PRE_RECEIVE_SCRIPT, encoding="utf-8", newline="\n")
    hook_path.chmod(0o755)  # Make executable
    return hook_path


__all__ = ["PRE_RECEIVE_SCRIPT", "install_hooks"]
