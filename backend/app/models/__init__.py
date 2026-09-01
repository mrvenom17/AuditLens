"""Model registry.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test schema creation both rely on.
"""

from app.models.audit import Audit, AuditAssignment, ClientProfileDocument
from app.models.corpus import ControlDefinition
from app.models.evaluation import ControlEvaluation, EvidenceFact
from app.models.evidence import EvidenceChunk, EvidenceDocument
from app.models.finding import Finding, FindingHistory, Report
from app.models.scoping import EvidenceRequest, ScopedControl
from app.models.user import LoginAttempt, Session, User

__all__ = [
    "Audit",
    "AuditAssignment",
    "ClientProfileDocument",
    "ControlDefinition",
    "ControlEvaluation",
    "EvidenceChunk",
    "EvidenceDocument",
    "EvidenceFact",
    "EvidenceRequest",
    "Finding",
    "FindingHistory",
    "LoginAttempt",
    "Report",
    "ScopedControl",
    "Session",
    "User",
]
