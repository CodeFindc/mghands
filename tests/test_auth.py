import asyncio

from mghands_gateway.auth import hash_password, hash_token
from mghands_gateway.models import AuthTokenRecord, UserRecord, UserRole, utc_now
from mghands_gateway.session_store import SessionStore


def test_user_password_is_hashed_and_login_lookup_round_trips(tmp_path) -> None:
    asyncio.run(_test_user_password_is_hashed_and_login_lookup_round_trips(tmp_path))


async def _test_user_password_is_hashed_and_login_lookup_round_trips(tmp_path) -> None:
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    password_hash = hash_password('password123')
    assert password_hash != 'password123'

    await store.create_user(
        UserRecord(
            username='admin',
            password_hash=password_hash,
            role=UserRole.ADMIN,
        )
    )
    user = await store.get_user_by_username('admin')

    assert user is not None
    assert user.username == 'admin'
    assert user.password_hash == password_hash


def test_token_store_keeps_only_hash(tmp_path) -> None:
    asyncio.run(_test_token_store_keeps_only_hash(tmp_path))


async def _test_token_store_keeps_only_hash(tmp_path) -> None:
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    token_value = 'mgh_plaintext-token'
    token_hash = hash_token(token_value)

    await store.create_token(
        AuthTokenRecord(
            user_id='usr_1',
            token_hash=token_hash,
            expires_at=utc_now(),
        )
    )
    token = await store.get_token_by_hash(token_hash)

    assert token is not None
    assert token.token_hash == token_hash
    assert token.token_hash != token_value
