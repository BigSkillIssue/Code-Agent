"""post_tool hook: append every file Forge changed to .forge/changes.log."""

import json
import sys
import time
from pathlib import Path

event = json.load(sys.stdin)  # the hook event: tool, args, ok, output, ...
path = sys.argv[1] if len(sys.argv) > 1 else event.get("args", {}).get("path", "?")
log = Path(".forge") / "changes.log"
with log.open("a", encoding="utf-8") as handle:
    handle.write(f"{time.strftime('%H:%M:%S')} {event['tool']} {path}\n")
