import asyncio
from pathlib import Path

import pytest

from mghands_gateway.models import (
    SandboxLeaseKind,
    SessionRecord,
    SessionStatus,
    UserSandboxRecord,
    UserSandboxStatus,
)
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


def test_user_sandbox_generation_and_execution_lease(tmp_path: Path) -> None:
    asyncio.run(_test_user_sandbox_generation_and_execution_lease(tmp_path))


async def _test_user_sandbox_generation_and_execution_lease(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    sandbox = await store.begin_user_sandbox_generation(
        UserSandboxRecord(
            user_id='usr_a',
            sandbox_id='mghands-user-usr_a',
            container_name='mghands-user-usr_a',
            api_key_ciphertext=b'encrypted',
            api_key_key_id='v1',
            generation=1,
            image_ref='sandbox:test',
        )
    )
    assert sandbox.generation == 1
    sandbox.status = UserSandboxStatus.DELETED
    await store.save_user_sandbox(sandbox)
    next_generation = await store.begin_user_sandbox_generation(
        sandbox.model_copy(update={'status': UserSandboxStatus.PROVISIONING})
    )
    assert next_generation.generation == 2

    first = await store.acquire_user_sandbox_lease(
        'usr_a', SandboxLeaseKind.EXECUTION, 'session-a', 60
    )
    second = await store.acquire_user_sandbox_lease(
        'usr_a', SandboxLeaseKind.EXECUTION, 'session-b', 60
    )
    assert first
    assert second is None
    assert not await store.release_user_sandbox_lease(
        'usr_a', SandboxLeaseKind.EXECUTION, 'wrong-token'
    )
    assert await store.release_user_sandbox_lease(
        'usr_a', SandboxLeaseKind.EXECUTION, first
    )
