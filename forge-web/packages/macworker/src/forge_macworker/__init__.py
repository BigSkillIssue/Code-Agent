"""Forge Web's Mac build worker (`forge-mac-worker`).

It runs on a Mac the server's admin provides, connects out to the server, and runs each Apple
build, test or screenshot job in a macOS VM of the job's project (Tart), or straight on the Mac
in direct mode (CI, a Mac of your own). Only `wire` is shared with the server.
"""

__version__ = "0.1.0"
