#!/usr/bin/env python3
"""Run dictionary release tests without networking or database connections."""
from pathlib import Path
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto'}:
        raise RuntimeError('Release tests cannot access network or databases')


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'src'))
    sys.addaudithook(deny_network)
    if (ROOT / '.local-development-only').exists():
        raise SystemExit('FAIL: local development marker found in release source')
    for path in (ROOT / 'src/dictapp').glob('*.py'):
        if 'from dictapp import local_dev' in path.read_text():
            raise SystemExit('FAIL: local-only runtime imported in release source')
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
