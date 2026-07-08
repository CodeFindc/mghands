import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

import bcrypt

from mghands_gateway.models import utc_now


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('ascii')


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('ascii'))
    except ValueError:
        return False


def generate_access_token() -> str:
    return 'mgh_' + secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    digest = hashlib.sha256(token.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def token_expiry(ttl_seconds: int):
    return utc_now() + timedelta(seconds=ttl_seconds)
