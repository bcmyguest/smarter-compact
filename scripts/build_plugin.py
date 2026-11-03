#!/usr/bin/env python3
"""Copy the plugin into a directory Cursor can load as a local plugin.

    python scripts/build_plugin.py --out ~/.cursor/plugins/local/

The JavaScript the hooks and the MCP server run already lives under plugin/, so the
copy is the plugin directory itself. The LiteLLM proxy stays in the clone: `smart-compact proxy`
finds it next to this script's repository, or via SMART_COMPACT_ROOT.
"""
import argparse
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(REPO, "dist"))
    args = parser.parse_args(argv)
    dest = os.path.join(os.path.abspath(args.out), "smart-compact")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(
        os.path.join(REPO, "plugin"),
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
