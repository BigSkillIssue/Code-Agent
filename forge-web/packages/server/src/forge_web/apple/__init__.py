"""Apple apps on Forge Web: Mac workers, build jobs, and who may use them.

Sandboxes ask for builds through the gateway (`sandbox_api`), Macs take the jobs through the
public API (`worker_api`), admins manage both (`admin`). The server never unpacks or runs what a
project sends: it stores the packed project, hands it to a Mac, and checks what comes back.
"""
