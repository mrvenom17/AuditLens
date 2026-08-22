"""Enumerations shared across models and schemas.

These mirror 03_DATA_MODEL.md exactly. They are `str`-valued so the same member
serialises straight into a Pydantic response and stores as a readable value in
Postgres — an audit database is read by humans during investigations, and an
integer enum would make that needlessly hard.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    auditor = "auditor"
    reviewer = "reviewer"
    admin = "admin"


class EntityType(StrEnum):
    merchant = "merchant"
    service_provider = "service_provider"


class MerchantLevel(StrEnum):
    one = "1"
    two = "2"
    three = "3"
    four = "4"


class EngagementStatus(StrEnum):
    """Lifecycle is one-way; `finalized` is terminal (03_DATA_MODEL.md)."""

    intake = "intake"
    scoping = "scoping"
    in_progress = "in_progress"
    finalized = "finalized"


class ScopeSource(StrEnum):
    ai_suggested = "ai_suggested"
    manual = "manual"


class EvidenceRequestStatus(StrEnum):
    draft = "draft"
    # Set manually by the auditor as a note-to-self. The system does not verify
    # actual sending and never sends anything itself (ADR-004).
    sent_externally = "sent_externally"
    received = "received"


class ExtractionStatus(StrEnum):
    processing = "processing"
    complete = "complete"
    extraction_failed = "extraction_failed"


class FindingStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    rejected = "rejected"


class ComplianceStatus(StrEnum):
    """The values both the AI may suggest and a human may set as final."""

    satisfied = "satisfied"
    partial = "partial"
    not_satisfied = "not_satisfied"
    not_applicable = "not_applicable"


class FindingAction(StrEnum):
    accept = "accept"
    edit = "edit"
    reject = "reject"
    override = "override"
