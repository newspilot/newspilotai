# SPDX-License-Identifier: AGPL-3.0-only
"""Publishing limits are optional site settings, not an approval check.

An auto approval no longer has to record "within_caps". Limits, if a site sets any, are applied when an article is
published (newspilot.publish_limits): an article over a limit waits instead of being held for a person.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE approvals DROP CONSTRAINT ck_approvals_auto_checks_passed;
    ALTER TABLE approvals ADD CONSTRAINT ck_approvals_auto_checks_passed CHECK (decision <> 'auto_approved'
        OR checklist @> '{"quality_gate": true, "information_gain": true, "images_licensed": true,
                          "sources_credited": true}'::jsonb);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE approvals DROP CONSTRAINT ck_approvals_auto_checks_passed;
    ALTER TABLE approvals ADD CONSTRAINT ck_approvals_auto_checks_passed CHECK (decision <> 'auto_approved'
        OR checklist @> '{"quality_gate": true, "information_gain": true, "images_licensed": true,
                          "sources_credited": true, "within_caps": true}'::jsonb) NOT VALID;
    """)
