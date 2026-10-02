import asyncio
import json

from memory.postgres import postgres_pool
from support_storage.migrations import migrate, verify_schema


async def main():
    async with postgres_pool("migration", autocommit=False) as pool:
        applied = await migrate(pool)
        await verify_schema(pool)
        print(json.dumps({"applied": applied, "status": "ready"}))


if __name__ == "__main__":
    try:
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    except Exception as exc:
        raise SystemExit(
            f"Migration failed: {type(exc).__name__}; check local configuration"
        ) from None
