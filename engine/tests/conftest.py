# SPDX-License-Identifier: AGPL-3.0-only
"""Database fixtures.

NEWSPILOT_TEST_ADMIN_URL is a superuser URL for a throwaway PostgreSQL server (CI uses a service container).
Each test session creates its own database plus an owner role and an application role, runs the migrations as the
owner, hands the tests an engine for the application role, and drops everything afterwards. Without the variable,
database tests are skipped.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from newspilot import migrations
from newspilot.db import make_engine, make_session_factory
from newspilot.db.engine import normalize_url
from newspilot.secret_store import LocalKeyProvider
from newspilot.tenancy import TenantContext, create_tenant

ADMIN_URL = normalize_url(os.environ.get("NEWSPILOT_TEST_ADMIN_URL", ""))


@pytest.fixture(scope="session")
def database():
    """{"owner_url": ..., "app_url": ..., "owner_role": ..., "app_role": ...} for a fresh, migrated database."""
    if not ADMIN_URL:
        pytest.skip("NEWSPILOT_TEST_ADMIN_URL is not set")
    run = secrets.token_hex(4)
    db_name, owner, app = f"np_test_{run}", f"np_owner_{run}", f"np_app_{run}"
    owner_pw, app_pw = secrets.token_hex(16), secrets.token_hex(16)

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"CREATE ROLE {owner} LOGIN PASSWORD '{owner_pw}'"))
        c.execute(text(f"CREATE ROLE {app} LOGIN PASSWORD '{app_pw}' NOSUPERUSER NOBYPASSRLS"))
        c.execute(text(f"CREATE DATABASE {db_name} OWNER {owner}"))

    base = make_url(ADMIN_URL).set(database=db_name)
    owner_url = base.set(username=owner, password=owner_pw).render_as_string(hide_password=False)
    app_url = base.set(username=app, password=app_pw).render_as_string(hide_password=False)
    migrations.upgrade(owner_url, app)

    yield {"owner_url": owner_url, "app_url": app_url, "owner_role": owner, "app_role": app}

    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"))
        c.execute(text(f"DROP ROLE IF EXISTS {app}"))
        c.execute(text(f"DROP ROLE IF EXISTS {owner}"))
    admin.dispose()


@pytest.fixture(scope="session")
def app_engine(database):
    # A small pool, so tests really do reuse pooled connections across tenants.
    engine = make_engine(database["app_url"], pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine(database):
    engine = make_engine(database["owner_url"])
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def sessions(app_engine):
    return make_session_factory(app_engine)


@pytest.fixture
def two_tenants(sessions):
    """Two fresh tenants, A and B, as TenantContexts."""
    run = secrets.token_hex(3)
    a = create_tenant(sessions, f"a-{run}", "Tenant A")
    b = create_tenant(sessions, f"b-{run}", "Tenant B")
    return TenantContext(a, actor="test:alice"), TenantContext(b, actor="test:bob")


@pytest.fixture
def keys():
    return LocalKeyProvider(secrets.token_bytes(32), key_id=f"test-{uuid.uuid4().hex[:6]}")
