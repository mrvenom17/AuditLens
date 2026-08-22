"""initial schema

Revision ID: 0872b4a86404
Revises:
Create Date: 2026-08-23 00:15:00.803739

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

revision: str = "0872b4a86404"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector must exist before any Vector column is created
    # (02_ARCHITECTURE.md §7.2). Autogenerate cannot infer this.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_login_attempts_email_created", "login_attempts", ["email", "created_at"], unique=False
    )
    op.create_table(
        "pci_requirements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("clause_id", sa.String(length=20), nullable=False),
        sa.Column("requirement_family", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("corpus_version", sa.String(length=40), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clause_id", "corpus_version", name="uq_clause_per_version"),
    )
    op.create_index(
        op.f("ix_pci_requirements_clause_id"), "pci_requirements", ["clause_id"], unique=False
    )
    op.create_index(
        op.f("ix_pci_requirements_corpus_version"),
        "pci_requirements",
        ["corpus_version"],
        unique=False,
    )
    op.create_index(
        "ix_pci_requirements_family", "pci_requirements", ["requirement_family"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "role", sa.Enum("auditor", "reviewer", "admin", name="user_role"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_table(
        "client_profile_documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("uploaded_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_client_profile_documents_content_hash"),
        "client_profile_documents",
        ["content_hash"],
        unique=False,
    )
    op.create_table(
        "engagements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column(
            "entity_type",
            sa.Enum("merchant", "service_provider", name="entity_type"),
            nullable=False,
        ),
        sa.Column(
            "merchant_level",
            sa.Enum("one", "two", "three", "four", name="merchant_level"),
            nullable=True,
        ),
        sa.Column("annual_transaction_volume", sa.Integer(), nullable=True),
        sa.Column("existing_saq_type", sa.String(length=20), nullable=True),
        sa.Column("tech_stack_summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("intake", "scoping", "in_progress", "finalized", name="engagement_status"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("finalized_by", sa.UUID(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["finalized_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_engagements_created_by"), "engagements", ["created_by"], unique=False)
    op.create_index(op.f("ix_engagements_status"), "engagements", ["status"], unique=False)
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_token_hash"), "sessions", ["token_hash"], unique=True)
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "engagement_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "user_id", name="uq_assignment"),
    )
    op.create_index(
        op.f("ix_engagement_assignments_engagement_id"),
        "engagement_assignments",
        ["engagement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_engagement_assignments_user_id"),
        "engagement_assignments",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_by", sa.UUID(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("pdf_path", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["generated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id"),
    )
    op.create_table(
        "scoped_requirements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=False),
        sa.Column("pci_requirement_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.Enum("ai_suggested", "manual", name="scope_source"), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("gap_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("gap_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["pci_requirement_id"], ["pci_requirements.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "pci_requirement_id", name="uq_scoped_requirement"),
    )
    op.create_index(
        op.f("ix_scoped_requirements_engagement_id"),
        "scoped_requirements",
        ["engagement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scoped_requirements_pci_requirement_id"),
        "scoped_requirements",
        ["pci_requirement_id"],
        unique=False,
    )
    op.create_table(
        "evidence_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=False),
        sa.Column("scoped_requirement_id", sa.UUID(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "sent_externally", "received", name="evidence_request_status"),
            nullable=False,
        ),
        sa.Column("description_source", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["scoped_requirement_id"], ["scoped_requirements.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_evidence_requests_engagement_id"),
        "evidence_requests",
        ["engagement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_requests_scoped_requirement_id"),
        "evidence_requests",
        ["scoped_requirement_id"],
        unique=False,
    )
    op.create_table(
        "findings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=False),
        sa.Column("scoped_requirement_id", sa.UUID(), nullable=False),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "evidence_document_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "ai_suggested_status",
            sa.Enum(
                "satisfied", "partial", "not_satisfied", "not_applicable", name="compliance_status"
            ),
            nullable=True,
        ),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("ai_rationale", sa.Text(), nullable=True),
        sa.Column("needs_manual_review", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "approved", "rejected", name="finding_status"),
            nullable=False,
        ),
        sa.Column(
            "final_status",
            sa.Enum(
                "satisfied", "partial", "not_satisfied", "not_applicable", name="compliance_status"
            ),
            nullable=True,
        ),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR (reviewed_by IS NOT NULL AND final_status IS NOT NULL)",
            name="ck_approved_requires_reviewer",
        ),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["scoped_requirement_id"], ["scoped_requirements.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_findings_engagement_id"), "findings", ["engagement_id"], unique=False)
    op.create_index(
        "ix_findings_engagement_status", "findings", ["engagement_id", "status"], unique=False
    )
    op.create_index(
        op.f("ix_findings_scoped_requirement_id"),
        "findings",
        ["scoped_requirement_id"],
        unique=False,
    )
    op.create_index(op.f("ix_findings_status"), "findings", ["status"], unique=False)
    op.create_table(
        "evidence_documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=False),
        sa.Column("evidence_request_id", sa.UUID(), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "extraction_status",
            sa.Enum("processing", "complete", "extraction_failed", name="extraction_status"),
            nullable=False,
        ),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extraction_error", sa.String(length=500), nullable=True),
        sa.Column("extraction_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extraction_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matching_status", sa.String(length=20), nullable=False),
        sa.Column("matching_error", sa.String(length=500), nullable=True),
        sa.Column("matching_attempts", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["evidence_request_id"], ["evidence_requests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_evidence_documents_content_hash"),
        "evidence_documents",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_documents_engagement_id"),
        "evidence_documents",
        ["engagement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_documents_evidence_request_id"),
        "evidence_documents",
        ["evidence_request_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_documents_status_created",
        "evidence_documents",
        ["extraction_status", "created_at"],
        unique=False,
    )
    op.create_table(
        "finding_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("finding_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column(
            "action",
            sa.Enum("accept", "edit", "reject", "override", name="finding_action"),
            nullable=False,
        ),
        sa.Column(
            "previous_status",
            sa.Enum("draft", "approved", "rejected", name="finding_status"),
            nullable=False,
        ),
        sa.Column(
            "new_status",
            sa.Enum("draft", "approved", "rejected", name="finding_status"),
            nullable=False,
        ),
        sa.Column(
            "previous_final_status",
            sa.Enum(
                "satisfied", "partial", "not_satisfied", "not_applicable", name="compliance_status"
            ),
            nullable=True,
        ),
        sa.Column(
            "new_final_status",
            sa.Enum(
                "satisfied", "partial", "not_satisfied", "not_applicable", name="compliance_status"
            ),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_finding_history_finding_id"), "finding_history", ["finding_id"], unique=False
    )
    op.create_table(
        "evidence_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("evidence_document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("location", sa.String(length=120), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["evidence_document_id"], ["evidence_documents.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_chunks_document",
        "evidence_chunks",
        ["evidence_document_id", "chunk_index"],
        unique=False,
    )
    # ### end Alembic commands ###

    # Vector indexes for the retrieval step (03_DATA_MODEL.md → PCIRequirement:
    # "vector index on embedding"). HNSW rather than IVFFlat: it needs no
    # training pass over existing rows, which matters because both tables start
    # empty and fill incrementally. Cosine distance matches the normalised
    # output of the BGE embedding model (ADR-005).
    op.execute(
        "CREATE INDEX ix_pci_requirements_embedding ON pci_requirements "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_evidence_chunks_embedding ON evidence_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evidence_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS ix_pci_requirements_embedding")

    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_evidence_chunks_document", table_name="evidence_chunks")
    op.drop_table("evidence_chunks")
    op.drop_index(op.f("ix_finding_history_finding_id"), table_name="finding_history")
    op.drop_table("finding_history")
    op.drop_index("ix_evidence_documents_status_created", table_name="evidence_documents")
    op.drop_index(
        op.f("ix_evidence_documents_evidence_request_id"), table_name="evidence_documents"
    )
    op.drop_index(op.f("ix_evidence_documents_engagement_id"), table_name="evidence_documents")
    op.drop_index(op.f("ix_evidence_documents_content_hash"), table_name="evidence_documents")
    op.drop_table("evidence_documents")
    op.drop_index(op.f("ix_findings_status"), table_name="findings")
    op.drop_index(op.f("ix_findings_scoped_requirement_id"), table_name="findings")
    op.drop_index("ix_findings_engagement_status", table_name="findings")
    op.drop_index(op.f("ix_findings_engagement_id"), table_name="findings")
    op.drop_table("findings")
    op.drop_index(
        op.f("ix_evidence_requests_scoped_requirement_id"), table_name="evidence_requests"
    )
    op.drop_index(op.f("ix_evidence_requests_engagement_id"), table_name="evidence_requests")
    op.drop_table("evidence_requests")
    op.drop_index(
        op.f("ix_scoped_requirements_pci_requirement_id"), table_name="scoped_requirements"
    )
    op.drop_index(op.f("ix_scoped_requirements_engagement_id"), table_name="scoped_requirements")
    op.drop_table("scoped_requirements")
    op.drop_table("reports")
    op.drop_index(op.f("ix_engagement_assignments_user_id"), table_name="engagement_assignments")
    op.drop_index(
        op.f("ix_engagement_assignments_engagement_id"), table_name="engagement_assignments"
    )
    op.drop_table("engagement_assignments")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_token_hash"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_engagements_status"), table_name="engagements")
    op.drop_index(op.f("ix_engagements_created_by"), table_name="engagements")
    op.drop_table("engagements")
    op.drop_index(
        op.f("ix_client_profile_documents_content_hash"), table_name="client_profile_documents"
    )
    op.drop_table("client_profile_documents")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
    op.drop_index("ix_pci_requirements_family", table_name="pci_requirements")
    op.drop_index(op.f("ix_pci_requirements_corpus_version"), table_name="pci_requirements")
    op.drop_index(op.f("ix_pci_requirements_clause_id"), table_name="pci_requirements")
    op.drop_table("pci_requirements")
    op.drop_index("ix_login_attempts_email_created", table_name="login_attempts")
    op.drop_table("login_attempts")
    # ### end Alembic commands ###

    # Autogenerate drops tables but leaves the enum types behind, which makes a
    # second `upgrade` fail with "type already exists". TASK-004 requires the
    # migration to apply cleanly up *and* down, so they are dropped explicitly.
    for enum_name in (
        "user_role",
        "entity_type",
        "merchant_level",
        "engagement_status",
        "scope_source",
        "evidence_request_status",
        "extraction_status",
        "finding_status",
        "compliance_status",
        "finding_action",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
