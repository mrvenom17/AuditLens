"""Applicability conditions, assessment procedures, company profile and evidence strength.

Revision ID: c3d4e5f6a7b8
Revises: b1f2c3d4e5a6
Create Date: 2026-09-01

Additive throughout. Every new column carries a server default, so existing rows
are valid the moment the migration lands and no backfill pass is needed.

The one judgement call is `scoped_controls.applicability_status` defaulting to
`UNDETERMINED` rather than `IN_SCOPE`. Rows scoped before this feature existed
were never evaluated against any condition, and `UNDETERMINED` says exactly
that. Defaulting them to `IN_SCOPE` would claim a determination that was never
made — and defaulting to `NOT_APPLICABLE` would silently drop controls out of
historical audits.

The `scope_source` enum gains its new member in a separate migration
(d4e5f6a7b8c9), because Postgres will not accept a newly-added enum value being
used in the same transaction that adds it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b1f2c3d4e5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- New enum types ---------------------------------------------------
    # Created explicitly so the column definitions below can reference them with
    # create_type=False; otherwise add_column re-emits CREATE TYPE and fails.
    op.execute(
        "CREATE TYPE applicability_status AS ENUM ('IN_SCOPE', 'NOT_APPLICABLE', 'UNDETERMINED')"
    )
    op.execute("CREATE TYPE evidence_strength AS ENUM ('STRONG', 'MODERATE', 'WEAK', 'NONE')")
    applicability_status = postgresql.ENUM(name="applicability_status", create_type=False)
    evidence_strength = postgresql.ENUM(name="evidence_strength", create_type=False)

    # --- Control corpus ---------------------------------------------------
    op.add_column(
        "control_definitions",
        sa.Column(
            "applicability_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "control_definitions",
        sa.Column(
            "assessment_procedures",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    # A STRUCTURED control with no declared facts checks nothing and would return
    # INSUFFICIENT_EVIDENCE forever — indistinguishable from missing evidence.
    op.create_check_constraint(
        "ck_structured_requires_facts",
        "control_definitions",
        "evaluation_mode <> 'STRUCTURED' OR jsonb_array_length(facts) > 0",
    )

    # --- Company profile --------------------------------------------------
    op.add_column(
        "audits",
        sa.Column(
            "company_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )

    # --- Applicability determination on scope -----------------------------
    op.add_column(
        "scoped_controls",
        sa.Column(
            "applicability_status",
            applicability_status,
            nullable=False,
            server_default="UNDETERMINED",
        ),
    )
    op.add_column(
        "scoped_controls",
        sa.Column(
            "applicability_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # --- Evidence strength on evaluations ---------------------------------
    op.add_column(
        "control_evaluations",
        sa.Column("evidence_strength", evidence_strength, nullable=False, server_default="NONE"),
    )
    op.add_column(
        "control_evaluations",
        sa.Column(
            "strength_factors",
            postgresql.ARRAY(sa.String(length=48)),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("control_evaluations", "strength_factors")
    op.drop_column("control_evaluations", "evidence_strength")
    op.drop_column("scoped_controls", "applicability_evidence")
    op.drop_column("scoped_controls", "applicability_status")
    op.drop_column("audits", "company_profile")
    op.drop_constraint("ck_structured_requires_facts", "control_definitions", type_="check")
    op.drop_column("control_definitions", "assessment_procedures")
    op.drop_column("control_definitions", "applicability_conditions")
    op.execute("DROP TYPE IF EXISTS evidence_strength")
    op.execute("DROP TYPE IF EXISTS applicability_status")
