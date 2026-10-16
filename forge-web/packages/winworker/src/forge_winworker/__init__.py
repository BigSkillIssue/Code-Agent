"""Forge Web's Windows build worker (`forge-win-worker`).

It runs on a Windows machine the server's admin provides, connects out to the server like the Mac
worker, and tests and packages the products' Windows apps as MSIX for the Microsoft Store. Only
`wire` is shared with the server.
"""

__version__ = "0.1.0"
