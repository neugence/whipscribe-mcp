#!/usr/bin/env python3
"""Thin entry-point wrapper for Claude Desktop local installs.

Claude Desktop resolves ``server.entry_point`` from the manifest and runs
this file directly.  It delegates immediately to the package's real entry
point so there is no logic duplication.
"""

from whipscribe_mcp.__main__ import main

if __name__ == "__main__":
    main()
