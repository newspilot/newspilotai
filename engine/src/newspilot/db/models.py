# SPDX-License-Identifier: AGPL-3.0-only
"""Tenancy-core tables. The schema itself is created by the Alembic migrations, which also add row-level
security, grants and the SQL functions. These models only map the tables for the application.

Rule: every table that holds tenant data has a non-null tenant_id and a forced row-level security policy.
tests/test_tenancy.py fails if a table does not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Tenant(Base):
    """One publication. The open-source edition runs with exactly one."""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(r"slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'", name="slug_format"),
        CheckConstraint("status IN ('active', 'paused', 'closed')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    # The publisher that takes editorial responsibility (shown on articles and the AI-use policy page).
    publisher_name: Mapped[str | None] = mapped_column(Text)
    publisher_contact: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now()


class TenantSetting(Base):
    """Non-secret configuration, one JSON value per key. Secrets go to TenantSecret, never here."""

    __tablename__ = "tenant_settings"
    __table_args__ = (CheckConstraint(r"key ~ '^[a-z][a-z0-9_.]{0,99}$'", name="key_format"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = _now()


class TenantKey(Base):
    """A tenant's data key, encrypted ("wrapped") by the master key. Versioned so keys can be rotated."""

    __tablename__ = "tenant_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    kek_id: Mapped[str] = mapped_column(String(100), nullable=False)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[dt.datetime] = _now()
    retired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class TenantSecret(Base):
    """An API key, CMS credential or token, encrypted with the tenant's data key. Never stored in plain text."""

    __tablename__ = "tenant_secrets"
    __table_args__ = (
        CheckConstraint(r"name ~ '^[a-z][a-z0-9_.]{0,99}$'", name="name_format"),
        CheckConstraint("status IN ('unchecked', 'valid', 'invalid')", name="status_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "key_version"], ["tenant_keys.tenant_id", "tenant_keys.version"], name="fk_secret_key"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # A keyed hash of the value: shows whether a re-entered value is the same, without revealing it.
    fingerprint: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unchecked")
    checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    check_message: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now()


class AuditEntry(Base):
    """Append-only record of who did what. The database refuses UPDATE and DELETE on this table."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    occurred_at: Mapped[dt.datetime] = _now()
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
