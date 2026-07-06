import asyncio
from pathlib import Path

import pytest

from mghands_gateway.models import SessionRecord, SessionStatus
from mghands_gateway.session_store import SessionStore


def test_session_store_round_trip(tmp_path: Path) -> None:
    asyncio.run(_test_session_store_round_trip(tmp_path))


async def _test_session_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    record = SessionRecord(session_id='tenant-a', sandbox_id='sandbox-1')

    await store.create(record)
    loaded = await store.require('tenant-a')

    assert loaded.session_id == 'tenant-a'
    assert loaded.sandbox_id == 'sandbox-1'


def test_session_store_updates_status(tmp_path: Path) -> None:
    asyncio.run(_test_session_store_updates_status(tmp_path))


async def _test_session_store_updates_status(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    record = await store.create(SessionRecord(session_id='tenant-a'))

    record.status = SessionStatus.RUNNING
    await store.save(record)

    loaded = await store.require('tenant-a')
    assert loaded.status == SessionStatus.RUNNING


def test_session_store_rejects_duplicate_session_id(tmp_path: Path) -> None:
    asyncio.run(_test_session_store_rejects_duplicate_session_id(tmp_path))


async def _test_session_store_rejects_duplicate_session_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    await store.create(SessionRecord(session_id='tenant-a'))

    with pytest.raises(KeyError):
        await store.create(SessionRecord(session_id='tenant-a'))


def test_session_store_preserves_sandbox_api_key(tmp_path: Path) -> None:
    asyncio.run(_test_session_store_preserves_sandbox_api_key(tmp_path))


async def _test_session_store_preserves_sandbox_api_key(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    await store.create(SessionRecord(session_id='tenant-a', sandbox_api_key='sk-test'))

    loaded = await store.require('tenant-a')
    assert loaded.sandbox_api_key is not None
    assert loaded.sandbox_api_key.get_secret_value() == 'sk-test'
