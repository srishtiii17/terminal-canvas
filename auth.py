# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast
# Implementation and development by Srishti Saluja
"""Optional shared-passphrase message authentication for protocol v2.

Every peer types the same passphrase. A 32-byte key is derived from it with
PBKDF2-HMAC-SHA256, and every v2 datagram then carries a 16-byte tag: the
first half of HMAC-SHA256(key, header + payload). The header's AUTH flag is
set, and it is covered by the tag like every other header byte. Receivers
recompute the tag and compare it with hmac.compare_digest (constant time).

What this provides: integrity (a modified frame is rejected) and group
authentication (only key holders can send accepted frames).

What it does NOT provide:

* confidentiality: frames are not encrypted and remain readable in
  Wireshark;
* per-peer authentication: any key holder can claim any peer ID;
* full replay protection: the duplicate tracker rejects recent repeats
  only, so an old frame replayed much later may be accepted.

The salt is fixed, because all peers must derive the same key without a
key-exchange step. The passphrase and the key are never logged or shown.
"""

from __future__ import annotations

import hashlib
import hmac

SALT = b"terminal-canvas/protocol-v2/hmac-sha256"
ITERATIONS = 200_000
KEY_SIZE = 32
TAG_SIZE = 16


class AuthKey:
    """A derived HMAC key. Its repr never reveals the key bytes."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != KEY_SIZE:
            raise ValueError(f"key must be {KEY_SIZE} bytes")
        self._key = key

    def tag(self, data: bytes) -> bytes:
        """The truncated HMAC-SHA256 tag of `data`."""
        return hmac.new(self._key, data, hashlib.sha256).digest()[:TAG_SIZE]

    def verify(self, data: bytes, tag: bytes) -> bool:
        """True if `tag` is the tag of `data` (constant-time comparison)."""
        return hmac.compare_digest(self.tag(data), tag)

    def __repr__(self) -> str:
        return "AuthKey(<hidden>)"

    __str__ = __repr__


def derive_key(passphrase: str, iterations: int = ITERATIONS) -> AuthKey:
    """Derive the group key from a passphrase with PBKDF2-HMAC-SHA256."""
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("the passphrase must not be empty")
    return AuthKey(hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), SALT, iterations, KEY_SIZE))
