import pytest

import app.database as database_module


class FakeConnection:
    def __init__(self):
        self.vector_extension_installed = False
        self.executed = []

    async def execute(self, sql, *_args):
        self.executed.append(sql)
        if "CREATE EXTENSION IF NOT EXISTS vector" in sql:
            self.vector_extension_installed = True

    async def fetchval(self, _sql, *_args):
        return False

    def transaction(self):
        return AsyncContext()


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _type, _value, _traceback):
        return False


class FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, _type, _value, _traceback):
        return False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_fresh_database_registers_vector_after_migration(monkeypatch):
    connection = FakeConnection()
    pool = FakePool(connection)
    registration_calls = []

    async def fake_register_vector(conn):
        registration_calls.append(conn.vector_extension_installed)
        if not conn.vector_extension_installed:
            raise ValueError("unknown type: public.vector")

    async def fake_create_pool(**kwargs):
        await kwargs["setup"](connection)
        return pool

    import pgvector.asyncpg
    monkeypatch.setattr(pgvector.asyncpg, "register_vector", fake_register_vector)
    monkeypatch.setattr(database_module.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(database_module, "validate_required_settings", lambda _settings: None)

    manager = database_module.DatabaseManager()
    await manager.connect()

    assert registration_calls == [False, True]
    assert manager.pool is pool
    assert manager._vector_bootstrap_pending == []
    await manager.disconnect()
    assert pool.closed
    assert manager.pool is None


@pytest.mark.asyncio
async def test_migration_failure_closes_pool(monkeypatch):
    connection = FakeConnection()
    pool = FakePool(connection)

    async def fake_register_vector(conn):
        if not conn.vector_extension_installed:
            raise ValueError("unknown type: public.vector")

    async def fake_create_pool(**kwargs):
        await kwargs["setup"](connection)
        return pool

    original_execute = connection.execute

    async def fail_on_migration(sql, *args):
        if "CREATE TABLE IF NOT EXISTS target_users" in sql:
            raise RuntimeError("fixture migration failure")
        await original_execute(sql, *args)

    connection.execute = fail_on_migration
    import pgvector.asyncpg
    monkeypatch.setattr(pgvector.asyncpg, "register_vector", fake_register_vector)
    monkeypatch.setattr(database_module.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(database_module, "validate_required_settings", lambda _settings: None)

    manager = database_module.DatabaseManager()
    with pytest.raises(RuntimeError, match="fixture migration failure"):
        await manager.connect()

    assert manager.pool is None
    assert pool.closed
