"""Migrate CB Viewer data from SQLite (bot.db) to PostgreSQL."""

import argparse
import asyncio
import os
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple, Type

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

try:
    from core.database import Base, AccountModel, EmployeeModel, SettingsModel, TargetModel
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Failed to import CB Viewer models: {exc}") from exc


MODEL_ORDER: Sequence[Tuple[Type, Sequence[str]]] = (
    (AccountModel, ("id",)),
    (SettingsModel, ("key",)),
    (EmployeeModel, ("id",)),
    (TargetModel, ("id",)),
)


def normalize_pg_url(raw: str) -> str:
    value = (raw or "").strip()
    if value.startswith("postgres://"):
        value = value.replace("postgres://", "postgresql://", 1)
    if value.startswith("postgresql://"):
        value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


def build_sqlite_url(path: Path) -> str:
    resolved = path.resolve()
    return f"sqlite+aiosqlite:///{resolved.as_posix()}"


def chunked(items: List[dict], chunk_size: int) -> Iterable[List[dict]]:
    for index in range(0, len(items), chunk_size):
        yield items[index : index + chunk_size]


def model_to_dict(model) -> dict:
    return {column.name: getattr(model, column.name) for column in model.__table__.columns}


async def fetch_rows(session: AsyncSession, model_type: Type) -> List[dict]:
    result = await session.execute(select(model_type))
    rows = result.scalars().all()
    return [model_to_dict(row) for row in rows]


async def upsert_rows(
    session: AsyncSession,
    model_type: Type,
    pk_columns: Sequence[str],
    rows: List[dict],
    chunk_size: int,
) -> int:
    if not rows:
        return 0

    total = 0
    for batch in chunked(rows, chunk_size):
        stmt = pg_insert(model_type.__table__).values(batch)
        update_cols = {
            column.name: getattr(stmt.excluded, column.name)
            for column in model_type.__table__.columns
            if column.name not in pk_columns
        }
        if update_cols:
            stmt = stmt.on_conflict_do_update(index_elements=list(pk_columns), set_=update_cols)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(pk_columns))
        await session.execute(stmt)
        total += len(batch)
    return total


async def migrate(sqlite_url: str, postgres_url: str, truncate: bool, chunk_size: int) -> None:
    source_engine = create_async_engine(sqlite_url)
    target_engine = create_async_engine(postgres_url)

    SourceSession = async_sessionmaker(source_engine, expire_on_commit=False, class_=AsyncSession)
    TargetSession = async_sessionmaker(target_engine, expire_on_commit=False, class_=AsyncSession)

    async with target_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SourceSession() as source, TargetSession() as target:
        if truncate:
            for model_type, _pk in MODEL_ORDER:
                await target.execute(delete(model_type))
            await target.commit()

        for model_type, pk_columns in MODEL_ORDER:
            rows = await fetch_rows(source, model_type)
            inserted = await upsert_rows(target, model_type, pk_columns, rows, chunk_size)
            await target.commit()
            print(f"{model_type.__tablename__}: {inserted} row(s) copied")

    await source_engine.dispose()
    await target_engine.dispose()


def resolve_postgres_url(arg_value: str) -> str:
    raw = (arg_value or "").strip()
    if not raw:
        raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raw = os.environ.get("POSTGRES_URL", "")
    raw = raw.strip()
    if not raw:
        raise SystemExit("PostgreSQL URL missing. Use --postgres or set DATABASE_URL.")
    normalized = normalize_pg_url(raw)
    if not normalized.startswith("postgresql+"):
        raise SystemExit("PostgreSQL URL must start with postgresql:// or postgresql+asyncpg://")
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate CB Viewer SQLite data to PostgreSQL.")
    parser.add_argument(
        "--sqlite",
        default="",
        help="Path to bot.db (default: ./data/bot.db)",
    )
    parser.add_argument(
        "--postgres",
        default="",
        help="PostgreSQL URL (postgresql://...)",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Delete target tables before copying.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="Rows per batch insert (default: 500)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    sqlite_path = Path(args.sqlite) if args.sqlite else (project_root / "data" / "bot.db")
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite db not found: {sqlite_path}")

    sqlite_url = build_sqlite_url(sqlite_path)
    postgres_url = resolve_postgres_url(args.postgres)

    asyncio.run(migrate(sqlite_url, postgres_url, args.truncate, max(1, args.chunk_size)))


if __name__ == "__main__":
    main()
