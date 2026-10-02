import hashlib
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
if not MIGRATIONS.exists():
    MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


async def migrate(pool):
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(60901001)")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS support_schema_versions (version TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = []
        for path in sorted(MIGRATIONS.glob("*.sql")):
            sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            sha = hashlib.sha256(sql.encode()).hexdigest()
            row = await (
                await conn.execute(
                    "SELECT sha256 FROM support_schema_versions WHERE version=%s", (path.stem,)
                )
            ).fetchone()
            if row:
                if row["sha256"] != sha:
                    raise ValueError("Applied business migration was modified")
            else:
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO support_schema_versions(version,sha256) VALUES (%s,%s)",
                    (path.stem, sha),
                )
                applied.append(path.stem)
        return applied


async def verify_schema(pool):
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT version,sha256 FROM support_schema_versions")
        ).fetchall()
        actual = {r["version"]: r["sha256"] for r in rows}
        expected = {
            p.stem: hashlib.sha256(
                p.read_text(encoding="utf-8").replace("\r\n", "\n").encode()
            ).hexdigest()
            for p in MIGRATIONS.glob("*.sql")
        }
        if not expected or actual != expected:
            raise RuntimeError("Business schema migration required")
