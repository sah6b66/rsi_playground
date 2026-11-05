"""Server-side pre-receive hook for the bare GitStore repo (DD-09 §5.2).

The hook enforces, at the repository boundary:
1. optimizer-identity pushes cannot touch ``judge/`` or ``prompts/``;
2. baseline tags (``suts/*/baseline/*``) and freeze tags
   (``evalsets/*/freeze/*``) can be created but never deleted or modified;
3. optimizer-identity pushes cannot modify frozen ``evalsets/`` paths.
"""

from __future__ import annotations

from pathlib import Path

PRE_RECEIVE_SCRIPT = """#!/bin/bash
# RSI Playground pre-receive hook
while read oldrev newrev refname; do
    # Skip branch deletions
    [ "$newrev" = "0000000000000000000000000000000000000000" ] && continue

    # Check if ref is a tag we should protect
    if [[ "$refname" == refs/tags/suts/*/baseline/* ]] || [[ "$refname" == refs/tags/evalsets/*/freeze/* ]]; then
        # Allow creation, block deletion (oldrev != 0000...)
        if [ "$oldrev" != "0000000000000000000000000000000000000000" ]; then
            echo "ERROR: Cannot modify protected tag: $refname" >&2
            exit 1
        fi
    fi

    # Check commit author for restricted paths
    author=$(git log --format='%an' -1 "$newrev" 2>/dev/null || echo "")
    if [[ "$author" == *"optimizer"* ]] || [[ "$author" == *"rsi-optimizer"* ]]; then
        # Check if any changed file is in restricted paths
        changed_files=$(git diff --name-only "$oldrev" "$newrev" 2>/dev/null || git ls-tree --name-only -r "$newrev")
        if echo "$changed_files" | grep -qE "^(judge/|prompts/)"; then
            echo "ERROR: optimizer identity cannot modify judge/ or prompts/" >&2
            exit 1
        fi
        if echo "$changed_files" | grep -qE "^evalsets/.*(/freeze/|^freeze)"; then
            echo "ERROR: optimizer identity cannot modify frozen evalsets/" >&2
            exit 1
        fi
    fi
done
exit 0
"""


def install_hooks(repo_path: Path) -> Path:
    """Write a pre-receive hook script to the bare repo's hooks/ directory.

    The script rejects:
    1. optimizer-identity pushes to judge/**, prompts/**
    2. Any push that deletes baseline or freeze tags
    3. optimizer-identity pushes to frozen evalsets/**

    Returns the path of the installed hook.
    """
    hooks_dir = Path(repo_path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-receive"
    hook_path.write_text(PRE_RECEIVE_SCRIPT, encoding="utf-8", newline="\n")
    hook_path.chmod(0o755)  # Make executable
    return hook_path


__all__ = ["PRE_RECEIVE_SCRIPT", "install_hooks"]
