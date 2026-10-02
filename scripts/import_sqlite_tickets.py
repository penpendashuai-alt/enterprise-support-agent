import argparse
import asyncio
import json
from pathlib import Path

from memory.postgres import postgres_pool
from support_storage.import_sqlite import import_records, read_source
from support_storage.migrations import verify_schema


async def run(args):
    if args.output.exists():
        raise ValueError("Preserve the previous import report")
    records = read_source(args.source)
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    async with postgres_pool("import", autocommit=False) as pool:
        await verify_schema(pool)
        report = await import_records(pool, records, mapping, apply=args.apply)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    try:
        asyncio.run(run(parser.parse_args()), loop_factory=asyncio.SelectorEventLoop)
    except Exception as exc:
        raise SystemExit(
            f"Import failed: {type(exc).__name__}; target batch rolled back, inspect source/mapping"
        ) from None
