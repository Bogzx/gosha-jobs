"""Delete the CV data of users inactive longer than the retention window.

The bot runs this daily (gosha/services/retention.py). Use this script to
see what it would do, or to run it by hand:

    python scripts/cv_retention.py --dry-run
    python scripts/cv_retention.py --months 18 --dry-run
    python scripts/cv_retention.py            # actually delete

In production, inside the bot container (same DATABASE_URL, key and
data/cvs mount as the service):

    docker compose -f docker-compose.prod.yml run --rm bot \\
      python scripts/cv_retention.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gosha import database  # noqa: E402
from gosha.services.retention import purge_stale_cvs, retention_months  # noqa: E402


async def main(months: int | None, dry_run: bool) -> int:
    await database.init_db(
        os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/jobs.db")
    )
    report = await purge_stale_cvs(months=months, dry_run=dry_run)
    for uid, last_seen in report.expired:
        seen = f"{last_seen:%Y-%m-%d}" if last_seen else "never"
        print(f"  user {uid:>6}  last active {seen}")
    print(report.summary())
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--months", type=int, default=None,
        help=f"retention window (default: CV_RETENTION_MONTHS or {retention_months()})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be deleted, delete nothing",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.months, args.dry_run)))
