"""Allow `python -m forge`; also how a sandboxed command starts under Landlock (`__landlock`)."""

import sys


def run() -> int:
    """Forge's command line, or the Landlock launcher without loading the rest of Forge."""
    if sys.argv[1:2] == ["__landlock"]:
        from forge.runtime.landlock import main as landlock_main

        return landlock_main(sys.argv[2:])
    from forge.cli import main

    return main()


raise SystemExit(run())
