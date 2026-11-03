#!/usr/bin/env python3
"""Deprecated: use `smarter-compact install cursor`."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "plugin", "scripts", "smarter-compact.mjs")


def main():
    if not os.path.isfile(CLI):
        print("smarter-compact install cursor  (from npm: npm install -g smarter-compact)", file=sys.stderr)
        return 1
    argv = ["install", "cursor"] + (["--copy"] if "--copy" in sys.argv[1:] else [])
    return subprocess.run([sys.executable, CLI, *argv], cwd=REPO).returncode


if __name__ == "__main__":
    sys.exit(main())
