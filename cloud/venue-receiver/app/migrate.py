"""Apply the idempotent receiver schema using the configured database."""

from __future__ import annotations

import asyncio

from .config import settings
from .repository import MySQLRepository


async def _main() -> None:
    settings.validate_runtime()
    repo = await MySQLRepository.connect(settings)
    await repo.close()
    print("venue receiver schema is up to date")


if __name__ == "__main__":
    asyncio.run(_main())
