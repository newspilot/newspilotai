# SPDX-License-Identifier: AGPL-3.0-only
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Checklist for a new table that holds tenant data: a NOT NULL tenant_id, ENABLE and FORCE ROW LEVEL SECURITY, a
tenant_isolation policy, and grants to the application role only for what it needs.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
