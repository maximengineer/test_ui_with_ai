"""`python -m dashboard.api` entry point.

Wraps `uvicorn.run(app)` with the bind-address policy from the plan:
default to `127.0.0.1`, opt into LAN binding via `AFR_DASHBOARD_BIND`,
log a loud warning when the bind is non-loopback.

Use `make dashboard-dev` (with --reload) for development. This entry
point is meant for production / Docker.
"""

from __future__ import annotations

import os
import sys

import uvicorn
from loguru import logger


_DEFAULT_PORT = 8080


def _resolve_port() -> int:
    """Read AFR_DASHBOARD_PORT and validate it. Hard-fail on bad input.

    Failing fast on a malformed env var is friendlier than uvicorn's later
    "[Errno 99] Cannot assign requested address" - the operator sees the
    actual problem with the actual variable name.
    """
    raw = os.environ.get("AFR_DASHBOARD_PORT")
    if raw is None or raw == "":
        return _DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.error(
            f"AFR_DASHBOARD_PORT={raw!r} is not an integer. "
            f"Unset it or provide a port in 1..65535."
        )
        sys.exit(2)
    if not (1 <= port <= 65535):
        logger.error(f"AFR_DASHBOARD_PORT={port} is out of range. Must be 1..65535.")
        sys.exit(2)
    return port


def main() -> None:
    bind = os.environ.get("AFR_DASHBOARD_BIND", "127.0.0.1")
    port = _resolve_port()

    if bind not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            f"dashboard binding to non-loopback address {bind!r}. The dashboard "
            "has no auth - anyone on the network can trigger runs. Set "
            "AFR_DASHBOARD_BIND=127.0.0.1 to revert."
        )

    uvicorn.run("dashboard.api:app", host=bind, port=port)


if __name__ == "__main__":
    main()
