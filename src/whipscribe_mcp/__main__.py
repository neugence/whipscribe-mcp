"""Entry point for ``uvx whipscribe-mcp`` / ``python -m whipscribe_mcp``."""

from __future__ import annotations

import sys

from .server import run_stdio


def main() -> None:
    try:
        run_stdio()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
