"""Encryption for secrets stored in the database (tenant GitHub tokens)."""

import os

from cryptography.fernet import Fernet, InvalidToken


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = os.getenv("SECRETS_KEY")
    if not key:
        raise RuntimeError("SECRETS_KEY is not set. Create one with: sourcer-admin generate-key")
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Could not decrypt: SECRETS_KEY is wrong or the value is corrupted") from e
