# SPDX-License-Identifier: AGPL-3.0-only
import newspilot


def test_version_is_set():
    assert newspilot.__version__


def test_plain_postgres_urls_use_psycopg3():
    from newspilot.db.engine import normalize_url

    assert normalize_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_url("postgres://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
