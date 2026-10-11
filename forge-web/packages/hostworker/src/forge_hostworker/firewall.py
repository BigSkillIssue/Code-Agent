"""The host's firewall for hosted apps: Docker's `DOCKER-USER` chain and the host's `INPUT`.

Docker puts every app network into one address pool (`deploy/host/daemon.json`), so the rules
can be fixed: app containers reach the internet but no private network, no cloud metadata
service, no mail port 25 and no other app; new outgoing connections are rate-limited against
scanning, and well-known mining pool ports are closed. Build containers (the `forge-build`
network) reach only DNS, HTTP and HTTPS. Traffic within one app's own network (its services
and its database, bridged) and answers to the edge pass. A second chain keeps containers off
the host's own services (SSH, Caddy's admin API, the worker). Root applies the rules
(`forge-host-worker firewall --apply`, before the worker starts).
"""

import subprocess
from collections.abc import Callable

Run = Callable[[list[str], str], int]  # argv, stdin -> exit code
APP_POOL = "10.88.0.0/16"  # deploy/host/daemon.json: default-address-pools
BUILD_SUBNET = "10.89.0.0/24"  # the forge-build network
CHAIN = "FORGE-EGRESS"  # jumped to from DOCKER-USER
HOST_CHAIN = "FORGE-HOST"  # jumped to from INPUT
SOURCES = (APP_POOL, BUILD_SUBNET)
PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
           "169.254.0.0/16", "224.0.0.0/4", "0.0.0.0/8")  # fmt: skip
MINING_PORTS = "3333,4444,5555,7777,8888,9999,14444,45700"
NEW_PER_SECOND = 20  # new outgoing connections per container and second
BURST = 100


def rules(allow: tuple[str, ...] = ()) -> list[str]:
    """The egress chain's rules, in iptables-save form; `allow` lets apps reach these addresses
    (Forge Web's relays on a private network, for example)."""
    lines = [f"-A {CHAIN} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
             f"-A {CHAIN} -m physdev --physdev-is-bridged -j RETURN"]  # fmt: skip
    for source in SOURCES:
        lines += [f"-A {CHAIN} -s {source} -d {address} -j RETURN" for address in allow]
        lines += [f"-A {CHAIN} -s {source} -d {network} -j DROP" for network in PRIVATE]
    lines += [
        f"-A {CHAIN} -s {BUILD_SUBNET} -p udp --dport 53 -j RETURN",
        f"-A {CHAIN} -s {BUILD_SUBNET} -p tcp -m multiport --dports 53,80,443 -j RETURN",
        f"-A {CHAIN} -s {BUILD_SUBNET} -j DROP",
        f"-A {CHAIN} -s {APP_POOL} -p tcp --dport 25 -j DROP",
        f"-A {CHAIN} -s {APP_POOL} -p tcp -m multiport --dports {MINING_PORTS} -j DROP",
        f"-A {CHAIN} -s {APP_POOL} -m conntrack --ctstate NEW -m hashlimit --hashlimit-above "
        f"{NEW_PER_SECOND}/sec --hashlimit-burst {BURST} --hashlimit-mode srcip "
        "--hashlimit-name forge-new -j DROP",
        f"-A {CHAIN} -j RETURN",
    ]
    return lines


def host_rules(allow: tuple[str, ...] = ()) -> list[str]:
    """The host chain's rules: containers may answer the host but not start talking to it."""
    lines = [f"-A {HOST_CHAIN} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"]
    for source in SOURCES:
        lines += [f"-A {HOST_CHAIN} -s {source} -d {address} -j RETURN" for address in allow]
        lines.append(f"-A {HOST_CHAIN} -s {source} -j DROP")
    return [*lines, f"-A {HOST_CHAIN} -j RETURN"]


def restore_script(allow: tuple[str, ...] = ()) -> str:
    """Input for `iptables-restore --noflush`: both chains made anew, nothing else touched."""
    return "\n".join([
        "*filter", f":{CHAIN} - [0:0]", f":{HOST_CHAIN} - [0:0]", f"-F {CHAIN}",
        f"-F {HOST_CHAIN}", *rules(allow), *host_rules(allow), "COMMIT", "",
    ])  # fmt: skip


def apply(allow: tuple[str, ...] = (), run: Run | None = None) -> list[str]:
    """Load the chains and jump to them from DOCKER-USER and INPUT (once); what it ran."""
    execute = run or _run
    if execute(["iptables-restore", "--noflush"], restore_script(allow)) != 0:
        raise OSError("iptables-restore refused the rules (is this root, with iptables?)")
    done = ["iptables-restore --noflush"]
    for jump in (["DOCKER-USER", "-j", CHAIN], ["INPUT", "-j", HOST_CHAIN]):
        if execute(["iptables", "-C", *jump], "") != 0:
            if execute(["iptables", "-I", *jump], "") != 0:
                raise OSError(f"iptables could not add the jump from {jump[0]}")
            done.append(f"iptables -I {' '.join(jump)}")
    return done


def _run(argv: list[str], stdin: str) -> int:
    """Run a command (as root) with this input; its exit code."""
    try:
        return subprocess.run(argv, input=stdin, text=True, check=False).returncode
    except FileNotFoundError:
        return 127
