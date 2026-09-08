"""Run ONLY under an OS-enforced deny-network/deny-old-root sandbox.

This probe deliberately fails if the OS rules are missing. It reads only a harmless
source manifest sentinel, never real credentials, and never sends an exchange request.
Usage: python tests/os_isolation_probe.py COPIED_MODULE OLD_REPO_PACKAGE_JSON
"""
import errno
import json
from pathlib import Path
import socket
import subprocess
import sys


def main():
    module, forbidden_manifest = map(Path, sys.argv[1:3])
    evidence = {'os_sandbox': {}, 'old_source_denied': False}
    for family, address, label in ((socket.AF_INET, ('127.0.0.1', 9), 'ipv4'),
                                    (socket.AF_INET6, ('::1', 9), 'ipv6')):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(address)
            except PermissionError as error:
                assert error.errno in (errno.EACCES, errno.EPERM)
                evidence['os_sandbox'][label] = 'PERMISSION_DENIED'
            else:
                raise AssertionError('Network not blocked by OS')
    try:
        forbidden_manifest.read_bytes()
    except PermissionError:
        evidence['old_source_denied'] = True
    else:
        raise AssertionError('Original source remains readable')
    # Both child and parent remain inside the same kernel sandbox policy.
    outcome = subprocess.run([sys.executable, str(module / 'main.py'), '--isolation-smoke'],
                             cwd=module.parent, capture_output=True, text=True, timeout=30)
    assert outcome.returncode == 0, outcome.stderr
    evidence['result'] = json.loads(outcome.stdout)
    assert evidence['result']['dry_run'] is True
    assert evidence['result']['trades_created'] == 1
    assert evidence['result']['network_clients_created'] == 0
    assert evidence['result']['private_requests'] == evidence['result']['live_orders'] == 0
    assert not (module / 'trades/live').exists()
    assert not (module / 'logs/live').exists()
    assert not (module / 'server').exists()
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    main()
