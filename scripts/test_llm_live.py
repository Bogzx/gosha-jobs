"""One-off live check of the configured LLM provider (.env credentials)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
# Runnable as `python scripts/<name>.py` from anywhere: that puts scripts/
# on sys.path, not the repo root, so `import gosha` would fail.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gosha import llm  # noqa: E402


async def main() -> None:
    print("provider:", llm.active_provider())
    result = await llm.generate(
        "Reply with exactly one short sentence confirming you are working."
    )
    print("result:", result)


if __name__ == "__main__":
    asyncio.run(main())
