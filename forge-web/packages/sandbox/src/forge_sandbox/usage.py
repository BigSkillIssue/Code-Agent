"""How much of the disk a workspace uses (regular files only; links are not followed)."""

import os
import stat
from typing import Any

from forge_sandbox.fsops import Workspace

MAX_ENTRIES = 2_000_000  # stop counting here (and say so)


def disk_usage(ws: Workspace, max_entries: int = MAX_ENTRIES) -> dict[str, Any]:
    """Bytes and number of the regular files in the workspace."""
    size = files = seen = 0
    stack = [str(ws.root)]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > max_entries:
                return {"bytes": size, "files": files, "complete": False}
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(entry.path)
            elif stat.S_ISREG(st.st_mode):
                size += st.st_size
                files += 1
    return {"bytes": size, "files": files, "complete": True}
