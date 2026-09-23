"""Restrict one private published API port without changing other host rules.

Run as root on the backend host. The raw PREROUTING rule matches the host
destination before Docker DNAT. Local host access is intentionally unaffected.
No chain is flushed and no default policy or unrelated rule is changed.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess


def rule(bind_ip: str, proxy_ip: str, port: int) -> list[str]:
    for value in (bind_ip, proxy_ip):
        address = ipaddress.ip_address(value)
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified:
            raise ValueError("API and proxy addresses must be private non-loopback IPv4 addresses")
    if bind_ip == proxy_ip:
        raise ValueError("Split hosts must have different addresses")
    if not 1024 <= port <= 65535:
        raise ValueError("API port must be an unprivileged TCP port")
    return ["-p", "tcp", "-d", f"{bind_ip}/32", "--dport", str(port), "!", "-s", f"{proxy_ip}/32",
            "-m", "comment", "--comment", "forgeseo-private-api", "-j", "DROP"]


def configure(action: str, bind_ip: str, proxy_ip: str, port: int, *, run=subprocess.run) -> bool:
    if action not in {"check", "apply", "remove"}:
        raise ValueError("Unknown firewall action")
    target = rule(bind_ip, proxy_ip, port)

    def command(operation: str):
        result = run(["iptables", "-w", "5", "-t", "raw", operation, "PREROUTING", *target],
                     capture_output=True, text=True, timeout=10)
        return result.returncode

    present = command("-C")
    if present not in (0, 1):
        raise RuntimeError("Could not inspect the API firewall rule")
    if action == "check":
        return present == 0
    if action == "apply" and present == 1:
        if command("-I") != 0:
            raise RuntimeError("Could not add the API firewall rule")
    if action == "remove" and present == 0:
        if command("-D") != 0:
            raise RuntimeError("Could not remove the exact API firewall rule")
    final = command("-C")
    if final not in (0, 1) or (final == 0) != (action == "apply"):
        raise RuntimeError("API firewall verification did not match the requested state")
    return True


def verify_local_address(bind_ip: str, *, run=subprocess.run) -> None:
    result = run(["ip", "-j", "-4", "address", "show"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError("Could not inspect backend network addresses")
    addresses = {address.get("local") for interface in json.loads(result.stdout)
                 for address in interface.get("addr_info", [])}
    if bind_ip not in addresses:
        raise ValueError("The specified API address does not belong to this host")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "remove"))
    parser.add_argument("--bind-ip", required=True)
    parser.add_argument("--proxy-ip", required=True)
    parser.add_argument("--port", type=int, default=18001)
    args = parser.parse_args()
    try:
        rule(args.bind_ip, args.proxy_ip, args.port)
        verify_local_address(args.bind_ip)
        if not configure(args.action, args.bind_ip, args.proxy_ip, args.port):
            print("The required API firewall rule is absent.")
            return 1
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"API firewall operation failed: {type(error).__name__}. No unrelated rules were requested.")
        return 2
    print(f"API firewall {args.action} verified. Only the selected destination and port are scoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
