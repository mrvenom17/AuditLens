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


class AuditStatus(StrEnum):
    """Lifecycle is one-way; `finalized` is terminal (03_DATA_MODEL.md)."""

    intake = "intake"
    scoping = "scoping"
    in_progress = "in_progress"
    finalized = "finalized"


class ScopeSource(StrEnum):
    """How a control came to be in scope.

    `deterministic` exists because labelling a mechanically-derived row
    `ai_suggested` would misattribute the decision — the auditor must be able to
    tell a rule's conclusion from a model's proposal at a glance.
    """

    deterministic = "deterministic"
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
    """03_DATA_MODEL.md § Finding (Redefined). `pending_review` replaces the
    prior revision's `draft`: a Finding now wraps an already-computed system
    result, so what is pending is the human decision, not the machine's."""

    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"
    needs_more_evidence = "needs_more_evidence"


class ComplianceStatus(StrEnum):
    """The values both the AI may suggest and a human may set as final."""

    satisfied = "satisfied"
    partial = "partial"
    not_satisfied = "not_satisfied"
    not_applicable = "not_applicable"


class FindingAction(StrEnum):
    """What the human did. Distinct from the six-state value they recorded:
    `approve` with a differing `auditor_decision` is an override of the machine,
    which 01_REQUIREMENTS.md requires be permitted and logged, never blocked."""

    approve = "approve"
    reject = "reject"
    request_more_evidence = "request_more_evidence"
    override = "override"


class EvaluationMode(StrEnum):
    """How a control is evaluated (01_REQUIREMENTS.md § Machine-Readable Control
    Definition).

    The distinction is load-bearing, not descriptive: only DETERMINISTIC and
    STRUCTURED controls are ever routed through the rule engine, and a
    HUMAN_ASSISTED control arriving there is a routing bug the Evidence Gate is
    required to catch (check 8).
    """

    DETERMINISTIC = "DETERMINISTIC"
    STRUCTURED = "STRUCTURED"
    HUMAN_ASSISTED = "HUMAN_ASSISTED"


class RuleOperator(StrEnum):
    """The fixed operator set. 01_REQUIREMENTS.md restricts rules to exactly
    these — an operator outside this set is rejected at authoring time, so the
    engine never meets one it cannot execute."""

    EQ = "=="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    IN = "IN"
    NOT_IN = "NOT_IN"
    CONTAINS = "CONTAINS"
    EXISTS = "EXISTS"
    NOT_EXISTS = "NOT_EXISTS"


class FactValueType(StrEnum):
    integer = "integer"
    boolean = "boolean"
    string = "string"
    date = "date"


class VerificationStatus(StrEnum):
    """A fact is VERIFIED only when its cited location is checkable — never
    because a model was confident (01_REQUIREMENTS.md § Fact Extraction,
    Explicitly Forbidden Behavior)."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class EvaluationResult(StrEnum):
    """The six-state system result. Replaces the binary pass/fail that the
    prior revision's ComplianceStatus expressed (00_PRODUCT.md §5.5).

    INSUFFICIENT_EVIDENCE and CONFLICT are complete, correct results — not
    error conditions to be retried (06_ENGINEERING_RULES.md § Error Handling).
    """

    PASS = "PASS"  # noqa: S105 — an evaluation result, not a credential
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICT = "CONFLICT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class GateStatus(StrEnum):
    """Outcome of the Evidence Gate's ten checks (01_REQUIREMENTS.md § Evidence
    Gate). REJECTED never becomes a silently-hidden Finding — it surfaces to the
    auditor as explicitly unverifiable."""

    VERIFIED = "VERIFIED"
    UNCERTAIN = "UNCERTAIN"
    REJECTED = "REJECTED"


class GateCheck(StrEnum):
    """The ten checks, named so a failure records *which* one failed rather than
    just that the gate said no (02_ARCHITECTURE.md §7.8)."""

    EVIDENCE_EXISTS = "EVIDENCE_EXISTS"
    BELONGS_TO_AUDIT = "BELONGS_TO_AUDIT"
    BELONGS_TO_DOCUMENT = "BELONGS_TO_DOCUMENT"
    LOCATION_VALID = "LOCATION_VALID"
    SUPPORTS_CLAIM = "SUPPORTS_CLAIM"
    FRESH = "FRESH"
    NO_CONTRADICTION = "NO_CONTRADICTION"
    VALID_EVALUATION_METHOD = "VALID_EVALUATION_METHOD"
    NO_INVENTED_FACTS = "NO_INVENTED_FACTS"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class MalwareScanStatus(StrEnum):
    """Level 0 decision (00_PRODUCT.md §5.8): recorded, not upload-gating.
    `not_scanned` is the honest default until a scanner is wired in."""

    not_scanned = "not_scanned"
    clean = "clean"
    flagged = "flagged"


class ApplicabilityStatus(StrEnum):
    """Whether a control applies to this company at all.

    `UNDETERMINED` is the load-bearing member. A condition referencing a profile
    attribute the company never answered must land here — never on
    `NOT_APPLICABLE`. Scoping a PCI control out because a form field was blank is
    the same "absence implies compliance" error the whole architecture exists to
    prevent, and it is the one failure here with legal consequence.
    """

    IN_SCOPE = "IN_SCOPE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNDETERMINED = "UNDETERMINED"


class EvidenceStrength(StrEnum):
    """How well-supported a result is, graded mechanically from provenance the
    system already holds (01_REQUIREMENTS.md § Finding Review).

    Ordered gates, not a weighted score: a threshold on a weighted sum is
    unauditable, and an auditor has to be able to see exactly why a grade was
    given. Nothing here consults a model.
    """

    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    NONE = "NONE"


class Industry(StrEnum):
    retail_ecommerce = "retail_ecommerce"
    retail_in_person = "retail_in_person"
    hospitality = "hospitality"
    financial_services = "financial_services"
    healthcare = "healthcare"
    saas = "saas"
    other = "other"


class Environment(StrEnum):
    on_premises = "on_premises"
    cloud = "cloud"
    hybrid = "hybrid"


class SystemComponent(StrEnum):
    ecommerce_platform = "ecommerce_platform"
    pos_terminals = "pos_terminals"
    call_centre = "call_centre"
    payment_gateway = "payment_gateway"
    internal_network = "internal_network"
    wireless_network = "wireless_network"
    custom_software = "custom_software"
    physical_facility = "physical_facility"


class DataType(StrEnum):
    pan = "pan"
    cardholder_name = "cardholder_name"
    expiry_date = "expiry_date"
    service_code = "service_code"
    sensitive_authentication_data = "sensitive_authentication_data"


class CloudProvider(StrEnum):
    aws = "aws"
    azure = "azure"
    gcp = "gcp"
    other = "other"
    none = "none"
