import os
from cryptography.fernet import Fernet

ENV_KEY_NAME = "PROVIDER_ENCRYPTION_KEY"
_cipher: Fernet | None = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is not None:
        return _cipher
    key = os.getenv(ENV_KEY_NAME)
    if not key:
        key = Fernet.generate_key().decode()
        os.environ[ENV_KEY_NAME] = key
    _cipher = Fernet(key.encode() if isinstance(key, str) else key)
    return _cipher


def encrypt_api_key(plaintext: str) -> str:
    cipher = _get_cipher()
    return cipher.encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    cipher = _get_cipher()
    return cipher.decrypt(ciphertext.encode()).decode()
