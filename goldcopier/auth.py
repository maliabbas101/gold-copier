"""Opaque server sessions, scrypt passwords and AES-256-GCM credential vault."""

import base64
import hashlib
import hmac
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return base64.b64encode(salt + key).decode()


def password_valid(password, encoded):
    try:
        raw = base64.b64decode(encoded)
        key = hashlib.scrypt(
            password.encode(), salt=raw[:16], n=16384, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(raw[16:], key)
    except (ValueError, TypeError):
        return False


class Vault:
    def __init__(self, key):
        raw = base64.urlsafe_b64decode(key)
        if len(raw) != 32:
            raise ValueError("VAULT_KEY must be a base64-encoded 32-byte key")
        self.aes = AESGCM(raw)

    def encrypt(self, plaintext, account_id):
        nonce = os.urandom(12)
        return base64.b64encode(
            nonce + self.aes.encrypt(nonce, plaintext.encode(), account_id.encode())
        ).decode()

    def decrypt(self, ciphertext, account_id):
        raw = base64.b64decode(ciphertext)
        return self.aes.decrypt(raw[:12], raw[12:], account_id.encode()).decode()
