"""Forge Web's Windows worker (`forge-win-worker`).

It runs on a Windows machine the server's admin provides, connects out to the server like the Mac
worker, and tests the products' Windows apps and packs them with Velopack into an installer, a
ZIP and update files for the products' websites; a signing machine of its own signs them. Only
`wire` is shared with the server.
"""

__version__ = "0.2.0"
