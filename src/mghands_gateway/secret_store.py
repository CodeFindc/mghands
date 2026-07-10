from cryptography.fernet import Fernet, InvalidToken

from mghands_gateway.config import Settings


class SecretCipher:
    def __init__(self, settings: Settings):
        self._keys = {
            key_id: Fernet(secret.get_secret_value().encode('ascii'))
            for key_id, secret in settings.gateway_secret_keys.items()
        }
        self.active_key_id = settings.gateway_active_secret_key_id

    def encrypt(self, value: str) -> tuple[bytes, str]:
        if not self.active_key_id or self.active_key_id not in self._keys:
            raise RuntimeError('gateway secret encryption is not configured')
        return self._keys[self.active_key_id].encrypt(value.encode('utf-8')), self.active_key_id

    def decrypt(self, ciphertext: bytes, key_id: str) -> str:
        cipher = self._keys.get(key_id)
        if cipher is None:
            raise RuntimeError(f'gateway secret key is unavailable: {key_id}')
        try:
            return cipher.decrypt(ciphertext).decode('utf-8')
        except InvalidToken as exc:
            raise RuntimeError('gateway secret could not be decrypted') from exc
