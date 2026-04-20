"""MCP server for Whipscribe transcription.

Beta service. See README and https://whipscribe.com/terms before use.
"""

from __future__ import annotations

import logging
import sys

import structlog

__version__ = "0.1.1"


def _configure_logging() -> None:
    # Stdio MCP transports reserve stdout for protocol frames. All logs must
    # go to stderr — writing to stdout would corrupt the MCP channel.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


_configure_logging()

__all__ = ["__version__"]
