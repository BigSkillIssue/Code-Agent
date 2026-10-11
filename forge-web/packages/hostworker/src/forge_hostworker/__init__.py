"""Forge Web's host worker (W25): runs hosted products on a Linux server of their own.

It connects out to the server, takes deploy jobs, and builds and runs each app only in gVisor
(`runsc`) containers, with a network and a PostgreSQL of its own. It never runs a product's
Dockerfile or compose file: the server sends a fully resolved `DeployPlan`.
"""

__version__ = "0.1.0"
