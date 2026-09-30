"""Re-embed every stored job and CV vector with the target model.

Part of switching SEMANTIC_MODEL (see docs/EMBEDDINGS.md). Batched and
resumable: each batch commits, and a rerun skips what is already done.

    python scripts/reembed.py --dry-run                   # what is stale
    python scripts/reembed.py                             # target = SEMANTIC_MODEL
    python scripts/reembed.py --model paraphrase-multilingual-mpnet-base-v2
    python scripts/reembed.py --jobs-only --batch 128

In production, inside the bot container (same DATABASE_URL, CV key and
data/cvs mount as the service, and its model cache):

    docker compose -f docker-compose.prod.yml run --rm \\
      -e SEMANTIC_MODEL=<new model> bot python scripts/reembed.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gosha import database, matching  # noqa: E402
from gosha.reembed import DEFAULT_BATCH, reembed  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    model = args.model or matching.DEFAULT_MODEL
    if model != matching.DEFAULT_MODEL:
        print(
            f"warning: target {model!r} differs from SEMANTIC_MODEL "
            f"({matching.DEFAULT_MODEL!r}); the running services only use "
            "vectors from their own SEMANTIC_MODEL.",
            file=sys.stderr,
        )
    await database.init_db(
        os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/jobs.db")
    )

    def progress(kind: str, n: int) -> None:
        print(f"  {kind}: {n} re-embedded", flush=True)

    report = await reembed(
        model,
        jobs=not args.cvs_only,
        cvs=not args.jobs_only,
        dry_run=args.dry_run,
        batch_size=args.batch,
        progress=progress,
    )
    print(report.summary())
    return 1 if report.cvs_failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model", help="target model (default: SEMANTIC_MODEL)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true", help="count only")
    only = parser.add_mutually_exclusive_group()
    only.add_argument("--jobs-only", action="store_true")
    only.add_argument("--cvs-only", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
