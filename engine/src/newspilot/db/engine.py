# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def normalize_url(url: str) -> str:
    """Accept plain postgres:// and postgresql:// URLs (as most hosts give them) and use the psycopg 3 driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str, **kwargs) -> Engine:
    """An engine for the application role. pool_pre_ping drops dead pooled connections."""
    kwargs.setdefault("pool_pre_ping", True)
    return create_engine(normalize_url(url), **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after the tenant transaction ends.
    return sessionmaker(bind=engine, expire_on_commit=False)
