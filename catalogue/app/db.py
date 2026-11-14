"""Apply Catalogue-owned SQL migrations without resetting existing records."""

import asyncio
from pathlib import Path

import asyncpg

from .config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


async def apply_migrations(
    connection: asyncpg.Connection, directory: Path = MIGRATIONS_DIR
) -> list[str]:
    migrations = sorted(directory.glob("[0-9]*_*.sql"))
    if not migrations:
        raise RuntimeError("No Catalogue migrations found")

    applied = []
    async with connection.transaction():
        schema = await connection.fetchval("SELECT current_schema()")
        # Serialize runners in this database/schema before creating the ledger.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"catalogue:migrations:{schema}",
        )
        await connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        existing = {
            row["version"]
            for row in await connection.fetch("SELECT version FROM schema_migrations")
        }
        for path in migrations:
            if path.name in existing:
                continue
            await connection.execute(path.read_text())
            await connection.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1)", path.name
            )
            applied.append(path.name)
    return applied


async def main() -> None:
    settings = Settings.from_env()
    connection = await asyncpg.connect(
        settings.database_url,
        timeout=settings.database_timeout_seconds,
        command_timeout=settings.database_timeout_seconds,
    )
    try:
        applied = await apply_migrations(connection)
        print("Applied migrations: " + ", ".join(applied) if applied else "Schema is up to date")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
