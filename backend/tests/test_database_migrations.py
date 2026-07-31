import asyncio
import sqlite3

import pytest

import database


class FakeMigrationDb:
    def __init__(self, error: sqlite3.OperationalError):
        self.error = error

    async def execute_fetchall(self, _query):
        return []

    async def execute(self, _query):
        raise self.error


def test_ensure_column_tolerates_parallel_duplicate_column_race():
    asyncio.run(
        database._ensure_column(
            FakeMigrationDb(
                sqlite3.OperationalError(
                    "duplicate column name: concurrent_field"
                )
            ),
            "customers",
            "concurrent_field",
            "TEXT",
        )
    )


def test_ensure_column_preserves_unrelated_sqlite_errors():
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        asyncio.run(
            database._ensure_column(
                FakeMigrationDb(
                    sqlite3.OperationalError("database is locked")
                ),
                "customers",
                "concurrent_field",
                "TEXT",
            )
        )
