# SPDX-License-Identifier: AGPL-3.0-only
"""Per-tenant secrets with envelope encryption.

- Each tenant has a random 256-bit data key. It is stored only in wrapped form, encrypted with the master key
  (the "key-encryption key"), in tenant_keys.
- Each secret is encrypted with AES-256-GCM under its tenant's data key. The additional authenticated data
  binds the ciphertext to its tenant and name, so a ciphertext copied to another tenant or another name fails to
  decrypt.
- A database dump on its own exposes nothing. Decryption also needs the master key, which lives outside the
  database: an environment variable for self-hosting (LocalKeyProvider), and a managed KMS for the hosted service
  (a KeyProvider with the same interface).

Values come back as SecretValue, whose repr and str are redacted, so a secret does not end up in a log line or
traceback by accident.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import secrets as _stdlib_secrets
import uuid
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from newspilot import audit
from newspilot.config import ConfigError
from newspilot.db.models import TenantKey, TenantSecret
from newspilot.tenancy import current_context

NONCE_BYTES = 12
KEY_BYTES = 32


class SecretError(RuntimeError):
    """A secret could not be stored or read (wrong master key, tampered data, missing data key)."""


class SecretValue:
    """A secret string that does not print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and hmac.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        return hash(("SecretValue", hashlib.sha256(self._value.encode()).digest()))


class KeyProvider(Protocol):
    """Wraps and unwraps tenant data keys with a master key that never enters the database."""

    @property
    def key_id(self) -> str: ...

    def wrap(self, data_key: bytes, aad: bytes) -> bytes: ...

    def unwrap(self, kek_id: str, wrapped: bytes, aad: bytes) -> bytes: ...


class LocalKeyProvider:
    """A master key held in memory, from NEWSPILOT_MASTER_KEY. Suitable for self-hosting."""

    def __init__(self, master_key: bytes, key_id: str = "local-1") -> None:
        if len(master_key) != KEY_BYTES:
            raise ConfigError("the master key must be exactly 32 bytes")
        self._aead = AESGCM(master_key)
        self._key_id = key_id

    @classmethod
    def from_env(cls) -> LocalKeyProvider:
        raw = os.environ.get("NEWSPILOT_MASTER_KEY", "").strip()
        if not raw:
            raise ConfigError("NEWSPILOT_MASTER_KEY is not set (create one with `newspilot secret new-master-key`)")
        try:
            key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError as e:
            raise ConfigError("NEWSPILOT_MASTER_KEY is not valid base64") from e
        return cls(key, os.environ.get("NEWSPILOT_MASTER_KEY_ID", "").strip() or "local-1")

    @property
    def key_id(self) -> str:
        return self._key_id

    def wrap(self, data_key: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, data_key, aad)

    def unwrap(self, kek_id: str, wrapped: bytes, aad: bytes) -> bytes:
        if kek_id != self._key_id:
            raise SecretError(f"data key was wrapped with master key {kek_id!r}, but {self._key_id!r} is loaded")
        try:
            return self._aead.decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], aad)
        except InvalidTag as e:
            raise SecretError("could not unwrap the tenant data key (wrong master key or tampered data)") from e


def new_master_key() -> str:
    """A fresh master key, base64-encoded, for NEWSPILOT_MASTER_KEY."""
    return base64.urlsafe_b64encode(_stdlib_secrets.token_bytes(KEY_BYTES)).decode().rstrip("=")


@dataclass(frozen=True)
class SecretInfo:
    """Everything about a secret except its value."""

    name: str
    status: str
    fingerprint: str
    updated_at: dt.datetime
    updated_by: str
    checked_at: dt.datetime | None
    check_message: str | None


def _key_aad(tenant_id: uuid.UUID, version: int) -> bytes:
    return f"newspilot:data-key:{tenant_id}:{version}".encode()


def _secret_aad(tenant_id: uuid.UUID, name: str) -> bytes:
    return f"newspilot:secret:{tenant_id}:{name}".encode()


class SecretStore:
    """Read and write the current tenant's secrets. Every method needs a session from tenant_session()."""

    def __init__(self, keys: KeyProvider | None) -> None:
        """keys may be None for read-only metadata (list); storing or reading values needs a KeyProvider."""
        self._keys_or_none = keys

    @property
    def _keys(self) -> KeyProvider:
        if self._keys_or_none is None:
            raise ConfigError("no master key loaded: set NEWSPILOT_MASTER_KEY")
        return self._keys_or_none

    # data keys

    def _active_key(self, session: Session, create: bool) -> tuple[int, bytes] | None:
        tenant_id = current_context(session).tenant_id
        row = session.scalars(
            select(TenantKey).where(TenantKey.retired_at.is_(None)).order_by(TenantKey.version.desc()).limit(1)
        ).first()
        if row is None:
            if not create:
                return None
            version = (session.scalar(select(func.max(TenantKey.version))) or 0) + 1
            data_key = AESGCM.generate_key(bit_length=256)
            session.add(
                TenantKey(
                    tenant_id=tenant_id,
                    version=version,
                    kek_id=self._keys.key_id,
                    wrapped_key=self._keys.wrap(data_key, _key_aad(tenant_id, version)),
                )
            )
            session.flush()
            return version, data_key
        return row.version, self._keys.unwrap(row.kek_id, row.wrapped_key, _key_aad(tenant_id, row.version))

    def _key(self, session: Session, version: int) -> bytes:
        tenant_id = current_context(session).tenant_id
        row = session.get(TenantKey, (tenant_id, version))
        if row is None:
            raise SecretError(f"data key version {version} is missing")
        return self._keys.unwrap(row.kek_id, row.wrapped_key, _key_aad(tenant_id, version))

    @staticmethod
    def _fingerprint(data_key: bytes, value: str) -> str:
        return hmac.new(data_key, value.encode(), hashlib.sha256).hexdigest()[:16]

    # secrets

    def put(self, session: Session, name: str, value: str | SecretValue) -> SecretInfo:
        """Store or replace a secret. It is marked "unchecked" until a provider check confirms it works."""
        plain = value.reveal() if isinstance(value, SecretValue) else value
        if not plain:
            raise ValueError("a secret cannot be empty")
        ctx = current_context(session)
        version, data_key = self._active_key(session, create=True)  # type: ignore[misc]
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(data_key).encrypt(nonce, plain.encode(), _secret_aad(ctx.tenant_id, name))
        fingerprint = self._fingerprint(data_key, plain)

        row = session.get(TenantSecret, (ctx.tenant_id, name))
        replaced = row is not None
        if row is None:
            row = TenantSecret(tenant_id=ctx.tenant_id, name=name)
            session.add(row)
        row.key_version = version
        row.nonce = nonce
        row.ciphertext = ciphertext
        row.fingerprint = fingerprint
        row.status = "unchecked"
        row.checked_at = None
        row.check_message = None
        row.updated_by = ctx.actor
        row.updated_at = func.now()
        session.flush()
        session.refresh(row)
        audit.record(session, "secret.replaced" if replaced else "secret.created", target_type="secret", target_id=name)
        return self._info(row)

    def get(self, session: Session, name: str) -> SecretValue | None:
        ctx = current_context(session)
        row = session.get(TenantSecret, (ctx.tenant_id, name))
        if row is None:
            return None
        data_key = self._key(session, row.key_version)
        try:
            plain = AESGCM(data_key).decrypt(row.nonce, row.ciphertext, _secret_aad(ctx.tenant_id, name))
        except InvalidTag as e:
            raise SecretError(f"secret {name!r} could not be decrypted (tampered or moved)") from e
        return SecretValue(plain.decode())

    def delete(self, session: Session, name: str) -> bool:
        ctx = current_context(session)
        row = session.get(TenantSecret, (ctx.tenant_id, name))
        if row is None:
            return False
        session.delete(row)
        session.flush()
        audit.record(session, "secret.deleted", target_type="secret", target_id=name)
        return True

    def list(self, session: Session) -> list[SecretInfo]:
        current_context(session)
        return [self._info(r) for r in session.scalars(select(TenantSecret).order_by(TenantSecret.name))]

    def mark_checked(self, session: Session, name: str, ok: bool, message: str = "") -> SecretInfo:
        """Record the result of testing a secret against its provider (for example, a key check on save)."""
        ctx = current_context(session)
        row = session.get(TenantSecret, (ctx.tenant_id, name))
        if row is None:
            raise KeyError(name)
        row.status = "valid" if ok else "invalid"
        row.checked_at = func.now()
        row.check_message = message[:500] or None
        session.flush()
        session.refresh(row)
        audit.record(session, "secret.checked", target_type="secret", target_id=name, details={"ok": ok})
        return self._info(row)

    @staticmethod
    def _info(row: TenantSecret) -> SecretInfo:
        return SecretInfo(
            name=row.name,
            status=row.status,
            fingerprint=row.fingerprint,
            updated_at=row.updated_at,
            updated_by=row.updated_by,
            checked_at=row.checked_at,
            check_message=row.check_message,
        )
