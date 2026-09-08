#!/usr/bin/env python3
"""Scheduled cleanup for inactive Atlas Slack worktrees on DigitalOcean."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from remote_helper import cleanup_stale
from router import FORCE_LOCAL_ENV, ROUTES_ENV, Router


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ttl-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.environ[ROUTES_ENV] = str(Path(args.config).expanduser().resolve())
    os.environ[FORCE_LOCAL_ENV] = "1"
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = cleanup_stale(Router(), ttl_hours=args.ttl_hours, dry_run=args.dry_run)
    print(json.dumps(summary, sort_keys=True, default=str))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
