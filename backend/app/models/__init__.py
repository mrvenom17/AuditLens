"""Model registry.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test schema creation both rely on.
"""

from app.models.corpus import PCIRequirement
from app.models.engagement import ClientProfileDocument, Engagement, EngagementAssignment
from app.models.evidence import EvidenceChunk, EvidenceDocument
from app.models.finding import Finding, FindingHistory, Report
from app.models.scoping import EvidenceRequest, ScopedRequirement
from app.models.user import LoginAttempt, Session, User

__all__ = [
    "ClientProfileDocument",
    "Engagement",
    "EngagementAssignment",
    "EvidenceChunk",
    "EvidenceDocument",
    "EvidenceRequest",
    "Finding",
    "FindingHistory",
    "LoginAttempt",
    "PCIRequirement",
    "Report",
    "ScopedRequirement",
    "Session",
    "User",
]
