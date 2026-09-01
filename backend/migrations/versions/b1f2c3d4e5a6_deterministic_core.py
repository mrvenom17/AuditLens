"""Deterministic evaluation core: Engagement→Audit rename, EvidenceFact,
ControlEvaluation, and the redefined Finding.

Revision ID: b1f2c3d4e5a6
Revises: 0872b4a86404
Create Date: 2026-08-31

This migration carries the whole retrofit described in 07_TASKS.md phases
R1-R4. It is written as a forward migration rather than by editing the initial
schema, because an already-deployed database has `engagements` tables that
Alembic would otherwise consider current — silently leaving it broken.

Three things happen here, in order:

1. **Rename.** `Engagement`→`Audit` throughout (03_DATA_MODEL.md), including the
   `engagement_status` enum type. Renames preserve data; they are not drops.

2. **New entities.** `evidence_facts` and `control_evaluations`, plus the new
   columns on `control_definitions` (`evaluation_mode`, `facts`, `rules`, …)
   that make a control machine-checkable at all.

3. **Finding redefinition, with a data migration (TASK-106).** Existing Findings
   carried an `ai_suggested_status` — an LLM's opinion stored as if it were a
   result. Those are not discarded, but neither are they laundered into the new
   model as though they had been mechanically determined. Each one gets a
   synthetic ControlEvaluation stamped `engine_version='legacy-llm-v0'` and
   `llm_involved=true`, so historical data stays readable and stays honestly
   labelled as having come from the pre-retrofit path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1f2c3d4e5a6"
down_revision: str | None = "0872b4a86404"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The old four-state vocabulary mapped onto the new six-state one. `satisfied`
# becomes PASS and `not_satisfied` FAIL; `partial` and `not_applicable` carry
# straight across. Nothing is invented — an old Finding with no suggestion at all
# maps to INSUFFICIENT_EVIDENCE, which is the truthful reading of "the model
# never produced one".
_LEGACY_RESULT_MAP = {
    "satisfied": "PASS",
    "partial": "PARTIAL",
    "not_satisfied": "FAIL",
    "not_applicable": "NOT_APPLICABLE",
}


def upgrade() -> None:
    # --- 1. Rename Engagement → Audit ------------------------------------
    op.execute("ALTER TYPE engagement_status RENAME TO audit_status")

    op.rename_table("engagements", "audits")
    op.rename_table("engagement_assignments", "audit_assignments")
    op.rename_table("pci_requirements", "control_definitions")
    op.rename_table("scoped_requirements", "scoped_controls")

    op.alter_column("audit_assignments", "engagement_id", new_column_name="audit_id")
    op.alter_column("scoped_controls", "engagement_id", new_column_name="audit_id")
    op.alter_column(
        "scoped_controls", "pci_requirement_id", new_column_name="control_definition_id"
    )
    op.alter_column("evidence_requests", "engagement_id", new_column_name="audit_id")
    op.alter_column(
        "evidence_requests", "scoped_requirement_id", new_column_name="scoped_control_id"
    )
    op.alter_column("evidence_documents", "engagement_id", new_column_name="audit_id")
    op.alter_column("findings", "engagement_id", new_column_name="audit_id")
    op.alter_column("findings", "scoped_requirement_id", new_column_name="scoped_control_id")
    op.alter_column("reports", "engagement_id", new_column_name="audit_id")

    # Corpus columns renamed to the vocabulary the rest of the system now uses.
    op.alter_column("control_definitions", "clause_id", new_column_name="control_id")
    op.alter_column("control_definitions", "title", new_column_name="name")
    op.alter_column("control_definitions", "full_text", new_column_name="requirement_text")

    # Postgres does not rename an index when its table or column is renamed, so
    # without this the schema would carry `ix_engagement_*` names forever and
    # every future autogenerate would report spurious drift.
    _RENAMED_INDEXES = {
        "ix_engagement_assignments_engagement_id": "ix_audit_assignments_audit_id",
        "ix_engagement_assignments_user_id": "ix_audit_assignments_user_id",
        "ix_engagements_created_by": "ix_audits_created_by",
        "ix_engagements_status": "ix_audits_status",
        "ix_pci_requirements_clause_id": "ix_control_definitions_control_id",
        "ix_pci_requirements_corpus_version": "ix_control_definitions_corpus_version",
        "ix_pci_requirements_family": "ix_control_definitions_family",
        "ix_pci_requirements_embedding": "ix_control_definitions_embedding",
        "ix_evidence_documents_engagement_id": "ix_evidence_documents_audit_id",
        "ix_evidence_requests_engagement_id": "ix_evidence_requests_audit_id",
        "ix_evidence_requests_scoped_requirement_id": "ix_evidence_requests_scoped_control_id",
        "ix_findings_engagement_id": "ix_findings_audit_id",
        "ix_findings_scoped_requirement_id": "ix_findings_scoped_control_id",
        "ix_scoped_requirements_engagement_id": "ix_scoped_controls_audit_id",
        "ix_scoped_requirements_pci_requirement_id": "ix_scoped_controls_control_definition_id",
    }
    for old_name, new_name in _RENAMED_INDEXES.items():
        op.execute(f"ALTER INDEX IF EXISTS {old_name} RENAME TO {new_name}")
    op.execute(
        "ALTER TABLE scoped_controls RENAME CONSTRAINT uq_scoped_requirement TO uq_scoped_control"
    )

    # --- 2. New enum types ------------------------------------------------
    # Created explicitly, once. The column definitions below then reference them
    # with create_type=False — otherwise create_table would re-emit CREATE TYPE
    # for each enum column and fail on the duplicate.
    op.execute(
        "CREATE TYPE evaluation_mode AS ENUM ('DETERMINISTIC', 'STRUCTURED', 'HUMAN_ASSISTED')"
    )
    op.execute(
        "CREATE TYPE evaluation_result AS ENUM "
        "('PASS', 'FAIL', 'PARTIAL', 'INSUFFICIENT_EVIDENCE', 'CONFLICT', 'NOT_APPLICABLE')"
    )
    op.execute("CREATE TYPE gate_status AS ENUM ('VERIFIED', 'UNCERTAIN', 'REJECTED')")
    op.execute("CREATE TYPE fact_value_type AS ENUM ('integer', 'boolean', 'string', 'date')")
    op.execute("CREATE TYPE verification_status AS ENUM ('VERIFIED', 'UNVERIFIED')")
    op.execute("CREATE TYPE malware_scan_status AS ENUM ('not_scanned', 'clean', 'flagged')")

    evaluation_mode = postgresql.ENUM(name="evaluation_mode", create_type=False)
    evaluation_result = postgresql.ENUM(name="evaluation_result", create_type=False)
    gate_status = postgresql.ENUM(name="gate_status", create_type=False)
    fact_value_type = postgresql.ENUM(name="fact_value_type", create_type=False)
    verification_status = postgresql.ENUM(name="verification_status", create_type=False)
    malware_scan_status = postgresql.ENUM(name="malware_scan_status", create_type=False)

    # --- 3. Machine-readable control corpus -------------------------------
    op.add_column(
        "control_definitions",
        sa.Column(
            "evaluation_mode",
            evaluation_mode,
            nullable=False,
            server_default="HUMAN_ASSISTED",
        ),
    )
    op.add_column(
        "control_definitions",
        sa.Column(
            "evidence_requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "control_definitions",
        sa.Column(
            "facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
    )
    op.add_column(
        "control_definitions",
        sa.Column(
            "rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
    )
    op.add_column(
        "control_definitions", sa.Column("freshness_window_days", sa.Integer(), nullable=True)
    )
    op.add_column(
        "control_definitions",
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_control_definitions_superseded_by",
        "control_definitions",
        "control_definitions",
        ["superseded_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_control_definitions_mode", "control_definitions", ["evaluation_mode"])
    # Belt and suspenders per TASK-102 — the service layer validates this too.
    op.create_check_constraint(
        "ck_deterministic_requires_rules",
        "control_definitions",
        "evaluation_mode <> 'DETERMINISTIC' "
        "OR (jsonb_array_length(rules) > 0 AND jsonb_array_length(facts) > 0)",
    )

    # --- 4. Audit + evidence additions ------------------------------------
    op.add_column(
        "audits",
        sa.Column("test_company", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "evidence_documents",
        sa.Column(
            "malware_scan_status",
            malware_scan_status,
            nullable=False,
            server_default="not_scanned",
        ),
    )

    # --- 5. EvidenceFact ---------------------------------------------------
    op.create_table(
        "evidence_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("control_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_type", fact_value_type, nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("cell", sa.String(length=40), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("extractor_version", sa.String(length=40), nullable=False),
        sa.Column("verification_status", verification_status, nullable=False),
        sa.ForeignKeyConstraint(["audit_id"], ["audits.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["control_definition_id"], ["control_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["evidence_documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # A VERIFIED fact must carry a checkable location (TASK-104).
        sa.CheckConstraint(
            "verification_status <> 'VERIFIED' "
            "OR (page IS NOT NULL OR line IS NOT NULL OR cell IS NOT NULL)",
            name="ck_verified_requires_location",
        ),
    )
    op.create_index("ix_evidence_facts_audit_id", "evidence_facts", ["audit_id"])
    op.create_index(
        "ix_evidence_facts_control_definition_id", "evidence_facts", ["control_definition_id"]
    )
    op.create_index("ix_evidence_facts_document_id", "evidence_facts", ["document_id"])
    op.create_index(
        "ix_evidence_facts_lookup",
        "evidence_facts",
        ["audit_id", "control_definition_id", "name"],
    )

    # --- 6. ControlEvaluation ---------------------------------------------
    op.create_table(
        "control_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("control_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result", evaluation_result, nullable=False),
        sa.Column("evaluation_mode", evaluation_mode, nullable=False),
        sa.Column(
            "facts_used",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "rules_used",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "evidence_locations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("contradictions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("gate_status", gate_status, nullable=False),
        sa.Column(
            "gate_checks_failed",
            postgresql.ARRAY(sa.String(length=40)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("engine_version", sa.String(length=40), nullable=False),
        sa.Column("llm_involved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["audit_id"], ["audits.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["control_definition_id"], ["control_definitions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "gate_status <> 'VERIFIED' OR array_length(gate_checks_failed, 1) IS NULL",
            name="ck_verified_gate_has_no_failures",
        ),
    )
    op.create_index("ix_control_evaluations_audit_id", "control_evaluations", ["audit_id"])
    op.create_index(
        "ix_control_evaluations_control_definition_id",
        "control_evaluations",
        ["control_definition_id"],
    )
    op.create_index(
        "ix_control_evaluations_audit",
        "control_evaluations",
        ["audit_id", "control_definition_id"],
    )

    # --- 7. Finding redefinition + data migration (TASK-106) --------------
    # The old CHECK references final_status, which is about to disappear.
    op.drop_constraint("ck_approved_requires_reviewer", "findings", type_="check")

    op.add_column(
        "findings",
        sa.Column("control_evaluation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("findings", sa.Column("ai_explanation", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("auditor_decision", evaluation_result, nullable=True))
    op.alter_column("findings", "scoped_control_id", nullable=True)

    # The status vocabulary changes shape, so the enum types are replaced
    # rather than extended. `ALTER TYPE ... ADD VALUE` cannot be used here:
    # Postgres forbids using a newly-added enum value in the same transaction
    # that adds it, and this migration writes 'pending_review' immediately.
    op.execute(
        "CREATE TYPE finding_status_new AS ENUM "
        "('pending_review', 'approved', 'rejected', 'needs_more_evidence')"
    )
    op.execute(
        "CREATE TYPE finding_action_new AS ENUM "
        "('approve', 'reject', 'request_more_evidence', 'override')"
    )

    # `draft` becomes `pending_review`: same meaning, honest new name — what is
    # pending is the human decision, not the machine's.
    status_cast = (
        "(CASE WHEN {col}::text = 'draft' THEN 'pending_review' "
        "ELSE {col}::text END)::finding_status_new"
    )
    op.execute(
        "ALTER TABLE findings ALTER COLUMN status DROP DEFAULT, "
        "ALTER COLUMN status TYPE finding_status_new USING " + status_cast.format(col="status")
    )
    for col in ("previous_status", "new_status"):
        op.execute(
            f"ALTER TABLE finding_history ALTER COLUMN {col} TYPE finding_status_new "
            "USING " + status_cast.format(col=col)
        )

    # `accept` becomes `approve`. `edit` — a human setting a value different
    # from the machine's — is exactly what the new model calls an override.
    op.execute(
        "ALTER TABLE finding_history ALTER COLUMN action TYPE finding_action_new USING "
        "(CASE action::text WHEN 'accept' THEN 'approve' WHEN 'edit' THEN 'override' "
        "ELSE action::text END)::finding_action_new"
    )

    op.execute("DROP TYPE finding_status")
    op.execute("DROP TYPE finding_action")
    op.execute("ALTER TYPE finding_status_new RENAME TO finding_status")
    op.execute("ALTER TYPE finding_action_new RENAME TO finding_action")
    op.execute(
        "ALTER TABLE findings ALTER COLUMN status SET DEFAULT 'pending_review'::finding_status"
    )

    # Build one synthetic ControlEvaluation per existing Finding, carrying the
    # old AI suggestion forward but stamped so it can never be mistaken for a
    # mechanically-determined result.
    case_sql = " ".join(
        f"WHEN f.ai_suggested_status::text = '{old}' THEN '{new_value}'"
        for old, new_value in _LEGACY_RESULT_MAP.items()
    )
    op.execute(
        f"""
        INSERT INTO control_evaluations (
            id, audit_id, control_definition_id, result, evaluation_mode,
            facts_used, rules_used, evidence_locations, contradictions, stale,
            gate_status, gate_checks_failed, evaluated_at, engine_version, llm_involved
        )
        SELECT
            gen_random_uuid(),
            f.audit_id,
            sc.control_definition_id,
            (CASE {case_sql} ELSE 'INSUFFICIENT_EVIDENCE' END)::evaluation_result,
            'HUMAN_ASSISTED'::evaluation_mode,
            '{{}}',
            '[]'::jsonb,
            COALESCE(f.citations, '[]'::jsonb),
            NULL,
            false,
            -- Never VERIFIED: the legacy path had no Evidence Gate, so no
            -- historical result can honestly claim to have passed one.
            'UNCERTAIN'::gate_status,
            '{{LEGACY_NO_GATE}}',
            f.created_at,
            'legacy-llm-v0',
            true
        FROM findings f
        JOIN scoped_controls sc ON sc.id = f.scoped_control_id
        """
    )

    # Link each Finding to the evaluation just created for it.
    op.execute(
        """
        UPDATE findings f
        SET control_evaluation_id = ce.id
        FROM control_evaluations ce
        WHERE ce.engine_version = 'legacy-llm-v0'
          AND ce.audit_id = f.audit_id
          AND ce.evaluated_at = f.created_at
          AND f.control_evaluation_id IS NULL
        """
    )

    # Carry the human's prior determination into its own column.
    decision_case = " ".join(
        f"WHEN final_status::text = '{old}' THEN '{new_value}'"
        for old, new_value in _LEGACY_RESULT_MAP.items()
    )
    op.execute(
        f"""
        UPDATE findings
        SET auditor_decision = (CASE {decision_case} ELSE NULL END)::evaluation_result
        WHERE final_status IS NOT NULL
        """
    )

    # Any Finding whose scope row had vanished gets no evaluation; it cannot be
    # represented in the new model and would violate the NOT NULL below.
    op.execute("DELETE FROM findings WHERE control_evaluation_id IS NULL")

    op.alter_column("findings", "control_evaluation_id", nullable=False)
    op.create_foreign_key(
        "fk_findings_control_evaluation",
        "findings",
        "control_evaluations",
        ["control_evaluation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_findings_control_evaluation_id", "findings", ["control_evaluation_id"])

    op.drop_column("findings", "ai_suggested_status")
    op.drop_column("findings", "ai_confidence")
    op.drop_column("findings", "ai_rationale")
    op.drop_column("findings", "needs_manual_review")
    op.drop_column("findings", "final_status")
    op.drop_column("findings", "citations")
    op.drop_column("findings", "evidence_document_ids")

    op.create_check_constraint(
        "ck_approved_requires_reviewer",
        "findings",
        "status <> 'approved' OR (reviewed_by IS NOT NULL AND auditor_decision IS NOT NULL)",
    )
    op.execute("ALTER INDEX ix_findings_engagement_status RENAME TO ix_findings_audit_status")

    # --- 8. FindingHistory ------------------------------------------------
    op.alter_column("finding_history", "previous_final_status", new_column_name="previous_decision")
    op.alter_column("finding_history", "new_final_status", new_column_name="new_decision")
    for col in ("previous_decision", "new_decision"):
        mapping = " ".join(
            f"WHEN {col}::text = '{old}' THEN '{new_value}'"
            for old, new_value in _LEGACY_RESULT_MAP.items()
        )
        op.execute(
            f"ALTER TABLE finding_history ALTER COLUMN {col} TYPE evaluation_result "
            f"USING (CASE {mapping} ELSE NULL END)::evaluation_result"
        )
    op.add_column("finding_history", sa.Column("system_result", evaluation_result, nullable=True))

    # --- 9. Report version stamps -----------------------------------------
    op.add_column("reports", sa.Column("corpus_version", sa.String(length=40), nullable=True))
    op.add_column("reports", sa.Column("engine_version", sa.String(length=40), nullable=True))

    # compliance_status is no longer referenced by any column.
    op.execute("DROP TYPE IF EXISTS compliance_status")


def downgrade() -> None:
    """Deliberately not implemented.

    This migration destroys information that cannot be reconstructed: an LLM's
    confidence score and free-text rationale are dropped, and the six-state
    result does not map back onto the old four-state enum without loss. A
    downgrade that silently invented those values would be worse than none.
    Restore from a backup instead (09_DEPLOYMENT.md).
    """
    raise NotImplementedError(
        "Downgrade is not supported for the deterministic-core migration; "
        "restore from a backup instead."
    )
