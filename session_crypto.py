"""Encrypt operator-sensitive identifiers before they are written to SQLite.

Facebook UIDs and session metadata identify a person. They are stored
encrypted so that a stolen database file does not expose them in clear text.

Design (no third-party dependency, standard primitives only):
  * Key derivation: PBKDF2-HMAC-SHA256(APP_SECRET, fixed salt, 120k rounds).
  * Confidentiality: HMAC-SHA256 counter-mode keystream (encrypt-then-MAC).
  * Integrity: HMAC-SHA256 over nonce||ciphertext, verified before decrypting.
  * Encoding: "v1.<b64 nonce>.<b64 ciphertext>.<b64 tag>".

The secret never leaves the process and is never written to disk or logs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache

VERSION = "v1"
SALT = b"fpm-session-v1"
ITERATIONS = 120_000
BLOCK = 32
NONCE_BYTES = 16
TAG_BYTES = 32


class DecryptError(ValueError):
    """Raised when a stored value is not authentic or was written by another secret."""


@lru_cache(maxsize=8)
def _key(secret: str) -> bytes:
    if not secret:
        raise DecryptError("Missing application secret")
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), SALT, ITERATIONS, dklen=32)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def encrypt(plaintext: str, secret: str) -> str:
    """Return an opaque token safe to store in the database."""
    if plaintext is None or plaintext == "":
        return ""
    key = _key(secret)
    nonce = os.urandom(NONCE_BYTES)
    raw = plaintext.encode("utf-8")
    ciphertext = _xor(raw, _keystream(key, nonce, len(raw)))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    encode = lambda blob: base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    return ".".join((VERSION, encode(nonce), encode(ciphertext), encode(tag)))


def decrypt(token: str, secret: str) -> str:
    """Return the original plaintext, or raise DecryptError when tampered."""
    if not token:
        return ""
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        raise DecryptError("Unsupported ciphertext format")
    decode = lambda text: base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    try:
        nonce, ciphertext, tag = decode(parts[1]), decode(parts[2]), decode(parts[3])
    except (ValueError, TypeError) as error:
        raise DecryptError("Malformed ciphertext") from error
    key = _key(secret)
    expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise DecryptError("Ciphertext failed integrity check")
    try:
        return _xor(ciphertext, _keystream(key, nonce, len(ciphertext))).decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecryptError("Ciphertext is not valid UTF-8") from error
