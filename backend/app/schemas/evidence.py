"""Evidence document schemas.

`storage_path` appears in no schema here. It is Sensitive (03_DATA_MODEL.md
§8.4), and a client that knows a server-side path knows something it can only
misuse.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.enums import ExtractionStatus
from app.schemas.common import ORMModel


class EvidenceDocumentSummary(ORMModel):
    """List shape — no extracted text.

    Excluded because it is Sensitive and because a sixty-document list would
    otherwise carry every document's full contents.
    """

    id: uuid.UUID
    engagement_id: uuid.UUID
    evidence_request_id: uuid.UUID | None
    original_filename: str
    content_hash: str
    mime_type: str
    size_bytes: int
    extraction_status: ExtractionStatus
    extraction_error: str | None
    matching_status: str
    uploaded_by: uuid.UUID
    created_at: datetime


class EvidenceDocumentDetail(EvidenceDocumentSummary):
    """Single-document shape, which does include the extracted text.

    Reachable only through the ownership-filtered single-document endpoint.
    """

    extracted_text: str | None
