/**
 * TypeScript mirrors of the backend Pydantic schemas.
 *
 * 06_ENGINEERING_RULES.md § Type Safety: "TypeScript types MUST mirror backend
 * schemas (kept in /frontend/types) — no `any` on data crossing the API
 * boundary."
 *
 * These are written by hand rather than generated, which means they can drift.
 * The mitigation is that every one names the backend schema it mirrors, so a
 * change on either side has an obvious counterpart to check. If drift ever
 * becomes a real problem, generate them from /openapi.json instead.
 */

export type Role = "auditor" | "reviewer" | "admin";

export type EntityType = "merchant" | "service_provider";
export type MerchantLevel = "1" | "2" | "3" | "4";

export type AuditStatus = "intake" | "scoping" | "in_progress" | "finalized";

/** How a control came to be in scope. `deterministic` is a rule's conclusion;
 *  `ai_suggested` is a model's advisory proposal. The UI must not present them
 *  as the same kind of claim. */
export type ScopeSource = "deterministic" | "ai_suggested" | "manual";

export type EvidenceRequestStatus = "draft" | "sent_externally" | "received";

export type ExtractionStatus = "processing" | "complete" | "extraction_failed";

export type FindingStatus =
  | "pending_review"
  | "approved"
  | "rejected"
  | "needs_more_evidence";

/**
 * The six-state system result (00_PRODUCT.md §5.5).
 *
 * Replaces the prior binary/four-state model. INSUFFICIENT_EVIDENCE and
 * CONFLICT are complete, correct answers — not errors and not "almost a pass".
 */
export type EvaluationResult =
  | "PASS"
  | "FAIL"
  | "PARTIAL"
  | "INSUFFICIENT_EVIDENCE"
  | "CONFLICT"
  | "NOT_APPLICABLE";

/** How a control is evaluated. Only the first two reach the rule engine. */
export type EvaluationMode = "DETERMINISTIC" | "STRUCTURED" | "HUMAN_ASSISTED";

/** The Evidence Gate's verdict on whether a result could be verified at all. */
export type GateStatus = "VERIFIED" | "UNCERTAIN" | "REJECTED";

/**
 * Whether a control applies to this company.
 *
 * UNDETERMINED is not a soft NOT_APPLICABLE — it means the company profile does
 * not answer a question the control's conditions ask, so the engine declined to
 * decide. Excluding a control on an unanswered question would remove a
 * requirement from the audit with nothing on screen to show for it.
 */
export type ApplicabilityStatus = "IN_SCOPE" | "NOT_APPLICABLE" | "UNDETERMINED";

/** How much weight the evidence behind a result can bear. Graded mechanically. */
export type EvidenceStrength = "STRONG" | "MODERATE" | "WEAK" | "NONE";

export type FindingAction =
  | "approve"
  | "reject"
  | "request_more_evidence"
  | "override";

/* --- Errors (02_ARCHITECTURE.md §7.7) --------------------------------------- */

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    request_id: string;
    /** Endpoint-specific detail: field errors, blocking requirements, retry_after. */
    [key: string]: unknown;
  };
}

export interface FieldError {
  field: string;
  reason: string;
}

/* --- Auth ------------------------------------------------------------------- */

/** Mirrors LoginResponse. */
export interface LoginResult {
  user_id: string;
  role: Role;
  name: string;
}

/** Mirrors CurrentUserResponse. */
export interface CurrentUser {
  user_id: string;
  email: string;
  name: string;
  role: Role;
}

/** Mirrors UserSummary. Carries no credential material by construction. */
export interface UserSummary {
  id: string;
  email: string;
  name: string;
  role: Role;
  is_active: boolean;
}

/* --- Audits ------------------------------------------------------------ */

/** Mirrors AuditSummary. Omits tech_stack_summary, which is Sensitive. */
export interface AuditSummary {
  id: string;
  client_name: string;
  entity_type: EntityType;
  merchant_level: MerchantLevel | null;
  status: AuditStatus;
  created_at: string;
  updated_at: string;
}

/** Mirrors AuditCounts. */
export interface AuditCounts {
  scoped_controls: number;
  confirmed_requirements: number;
  evidence_requests: number;
  evidence_documents: number;
  findings_total: number;
  findings_draft: number;
  findings_approved: number;
  findings_rejected: number;
  findings_needing_manual_review: number;
}

/** Mirrors AuditDetail. */
export interface AuditDetail {
  id: string;
  client_name: string;
  entity_type: EntityType;
  merchant_level: MerchantLevel | null;
  annual_transaction_volume: number | null;
  existing_saq_type: string | null;
  tech_stack_summary: string | null;
  status: AuditStatus;
  created_by: string;
  finalized_by: string | null;
  finalized_at: string | null;
  created_at: string;
  updated_at: string;
  counts: AuditCounts;
  assigned_user_ids: string[];
}

/** Mirrors AuditCreate. */
export interface AuditCreate {
  client_name: string;
  entity_type: EntityType;
  merchant_level?: MerchantLevel | null;
  annual_transaction_volume?: number | null;
  existing_saq_type?: string | null;
  tech_stack_summary?: string | null;
  source_document_ids?: string[];
}

/** Mirrors Page[T]. */
export interface Page<T> {
  items: T[];
  total: number;
}

/* --- Scoping ---------------------------------------------------------------- */

/** Mirrors ScopedRequirementResponse. */
export interface ScopedControl {
  id: string;
  audit_id: string;
  control_definition_id: string;
  control_id: string;
  title: string;
  requirement_family: number;
  source: ScopeSource;
  confirmed: boolean;
  rationale: string | null;
  gap_acknowledged: boolean;
  gap_note: string | null;
  applicability_status: ApplicabilityStatus;
  applicability_evidence: Array<Record<string, unknown>> | null;
  created_at: string;
  updated_at: string;
}

/** Mirrors CompanyProfile. Every field optional on purpose: an omitted key means
 *  "not answered" and yields UNDETERMINED, while an empty array means "asked,
 *  none apply" and is a real answer that may exclude a control. */
export interface CompanyProfile {
  industry?: string | null;
  environment?: string | null;
  systems?: string[] | null;
  data_types?: string[] | null;
  cloud_providers?: string[] | null;
  stores_cardholder_data?: boolean | null;
  transmits_cardholder_data?: boolean | null;
  outsources_card_processing?: boolean | null;
}

/** Mirrors ScopeSuggestionResponse. */
export interface ScopeSuggestion {
  proposed_requirements: ScopedControl[];
  /** True when the LLM was unavailable. A degraded success, never an error. */
  manual_scoping_required: boolean;
  saq_type: string | null;
  ambiguous_entity_type: boolean;
}

/* --- Evidence --------------------------------------------------------------- */

/** Mirrors EvidenceRequestResponse. */
export interface EvidenceRequest {
  id: string;
  audit_id: string;
  scoped_control_id: string;
  control_id: string;
  description: string;
  status: EvidenceRequestStatus;
  /** "llm" or "template" — records whether drafting degraded. */
  description_source: string;
  created_at: string;
  updated_at: string;
}

/** Mirrors EvidenceRequestGenerateResponse. */
export interface EvidenceRequestGenerateResult {
  created: EvidenceRequest[];
  skipped_already_requested: number;
  llm_available: boolean;
}

/** Mirrors EvidenceDocumentSummary. Never carries storage_path. */
export interface EvidenceDocumentSummary {
  id: string;
  audit_id: string;
  evidence_request_id: string | null;
  original_filename: string;
  content_hash: string;
  mime_type: string;
  size_bytes: number;
  extraction_status: ExtractionStatus;
  extraction_error: string | null;
  matching_status: string;
  uploaded_by: string;
  created_at: string;
}

/** Mirrors EvidenceDocumentDetail. */
export interface EvidenceDocumentDetail extends EvidenceDocumentSummary {
  extracted_text: string | null;
}

/* --- Findings --------------------------------------------------------------- */

/** Mirrors Citation — enough provenance to open the document and check it. */
export interface Citation {
  fact: string | null;
  value: string | null;
  evidence_document_id: string | null;
  /** SHA-256 of the document as extracted. Null on citations written before
   *  hashes were carried through. */
  source_hash: string | null;
  location: string | null;
  page: number | null;
  line: number | null;
  cell: string | null;
}

/**
 * Mirrors FindingResponse.
 *
 * The machine's fields and the human's are separate and are never merged.
 * `system_result` comes from the rule engine and is immutable;
 * `auditor_decision` is the human's and is null until they rule. A client
 * physically cannot receive them pre-merged, so it cannot render one as the
 * other by reading a single field (04_API_CONTRACT.md, Security Notes).
 */
export interface Finding {
  id: string;
  audit_id: string;
  control_evaluation_id: string;
  scoped_control_id: string | null;
  control_id: string;
  control_name: string;
  requirement_text: string;
  assessment_procedures: string[];

  /** What the machine determined, mechanically. Read-only, always. */
  system_result: EvaluationResult;
  evaluation_mode: EvaluationMode;
  gate_status: GateStatus;
  gate_checks_failed: string[];
  rules_used: Array<Record<string, unknown>>;
  evidence_locations: Citation[];
  contradictions: Array<Record<string, unknown>> | null;
  stale_evidence: boolean;
  evidence_strength: EvidenceStrength;
  /** Which rubric criteria fired. Shown so a grade is explained, not asserted. */
  strength_factors: string[];
  engine_version: string;
  /** False for every deterministic result. Surfaced so the UI can say so. */
  llm_involved: boolean;

  /** GenAI prose. Explicitly non-authoritative wherever displayed. */
  ai_explanation: string | null;

  /** What a human decided. Null until reviewed. */
  status: FindingStatus;
  auditor_decision: EvaluationResult | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;

  created_at: string;
  updated_at: string;

  /** Server-computed. True while no human has ruled on this finding. */
  awaiting_review: boolean;
  /** Server-computed. True when the human disagreed with the machine. */
  is_override: boolean;
  /**
   * Server-computed. True when the Evidence Gate could not verify this result.
   * Returned explicitly rather than left for the client to infer from
   * `gate_status`, so "this one is unverified" is a contract guarantee rather
   * than a styling convention (01_REQUIREMENTS.md § Finding Review, Edge Cases).
   */
  unverified_by_gate: boolean;
}

/** Mirrors FindingReviewRequest. Deliberately has no reviewed_by field, and
 *  deliberately no system_result — there is no name a client could send that
 *  would overwrite the machine's determination. */
export interface FindingReviewRequest {
  action: Exclude<FindingAction, "override">;
  /** Omit on approve to mean "I agree with the system result". */
  auditor_decision?: EvaluationResult | null;
  note?: string | null;
}

/** Mirrors FindingHistoryEntry. */
export interface FindingHistoryEntry {
  id: string;
  finding_id: string;
  actor_id: string;
  action: FindingAction;
  previous_status: FindingStatus;
  new_status: FindingStatus;
  previous_decision: EvaluationResult | null;
  new_decision: EvaluationResult | null;
  /** What the machine said at the moment of this decision. */
  system_result: EvaluationResult | null;
  note: string | null;
  created_at: string;
}

/* --- Finalization ----------------------------------------------------------- */

/** Mirrors BlockingRequirement. */
export interface BlockingRequirement {
  scoped_control_id: string;
  control_id: string;
  reason: string;
}

/** Mirrors FinalizationReadiness. */
export interface FinalizationReadiness {
  ready: boolean;
  blocking_requirements: BlockingRequirement[];
}

/** Mirrors FinalizeResponse. */
export interface FinalizeResult {
  report_id: string;
  audit_status: string;
}

/**
 * The immutable snapshot inside a Report, as built by
 * FinalizationService._build_snapshot.
 *
 * 03_DATA_MODEL.md makes this a full copy rather than a set of references, so
 * that a finalized report keeps saying what it said on the day it was signed
 * even if the corpus is later re-versioned. The UI reads the snapshot and never
 * re-derives any of it from live tables, for the same reason.
 */
export interface ReportSnapshotFinding {
  control_id: string;
  requirement_family: number;
  name: string;
  requirement_text: string;
  evaluation_mode: EvaluationMode;

  /** What the engine determined, and the mechanics that produced it. */
  system_result: EvaluationResult | null;
  gate_status: GateStatus | null;
  gate_checks_failed: string[];
  rules_used: Array<Record<string, unknown>>;
  evidence_locations: Citation[];
  contradictions: Array<Record<string, unknown>> | null;
  stale_evidence: boolean;
  engine_version: string | null;
  llm_involved_in_result: boolean | null;

  /** What the human decided. Both are kept; neither replaces the other. */
  auditor_decision: EvaluationResult | null;
  is_override: boolean;
  review_note: string | null;
  ai_explanation: string | null;

  reviewed_by: string;
  reviewed_at: string | null;
}

export interface ReportSnapshotGap {
  control_id: string;
  name: string;
  gap_note: string | null;
}

export interface ReportSnapshot {
  audit: {
    id: string;
    client_name: string;
    entity_type: EntityType;
    merchant_level: MerchantLevel | null;
    existing_saq_type: string | null;
  };
  framework: string;
  corpus_versions: string[];
  /** Stamped so a later engine change cannot rewrite what this report claims. */
  engine_versions: string[];
  /** Where GenAI was involved, and in what role. Always non-authoritative. */
  ai_disclosure: {
    model: string;
    prompt_version: string;
    role: string;
    authoritative: boolean;
  };
  generated_at: string;
  generated_by: { id: string; name: string; role: Role };
  findings: ReportSnapshotFinding[];
  acknowledged_gaps: ReportSnapshotGap[];
  rejected_finding_count: number;
  summary: {
    confirmed_requirements: number;
    approved_findings: number;
    acknowledged_gaps: number;
  };
}

/** Mirrors ReportResponse. */
export interface Report {
  id: string;
  audit_id: string;
  generated_by: string;
  generated_at: string;
  snapshot_data: ReportSnapshot;
}

/* --- Display helpers -------------------------------------------------------- */

export const RESULT_LABELS: Record<EvaluationResult, string> = {
  PASS: "Pass",
  FAIL: "Fail",
  PARTIAL: "Partial / exception",
  INSUFFICIENT_EVIDENCE: "Insufficient evidence",
  CONFLICT: "Conflicting evidence",
  NOT_APPLICABLE: "Not applicable",
};

export const FINDING_STATUS_LABELS: Record<FindingStatus, string> = {
  pending_review: "Awaiting review",
  approved: "Approved",
  rejected: "Rejected",
  needs_more_evidence: "More evidence requested",
};

/** Plain-English rendering of the Evidence Gate's named checks, so an auditor
 *  reads what could not be verified rather than an enum. */
export const STRENGTH_LABELS: Record<EvidenceStrength, string> = {
  STRONG: "Strong",
  MODERATE: "Moderate",
  WEAK: "Weak",
  NONE: "No evidence",
};

export const APPLICABILITY_LABELS: Record<ApplicabilityStatus, string> = {
  IN_SCOPE: "Applies",
  NOT_APPLICABLE: "Not applicable",
  UNDETERMINED: "Profile question unanswered",
};

/** Plain-English rendering of the strength rubric's factors, so an auditor reads
 *  why a grade was given rather than an enum. */
export const STRENGTH_FACTOR_LABELS: Record<string, string> = {
  no_supporting_facts: "no supporting facts were found",
  gate_not_verified: "the evidence gate could not verify this",
  unverified_fact: "a fact could not be verified against its source",
  stale_evidence: "the evidence is past this control's freshness window",
  contradictory_evidence: "documents disagree",
  precise_citations: "every citation points at a specific line or cell",
  well_inside_freshness_window: "the evidence is comfortably current",
  no_freshness_window: "this control declares no freshness window",
  single_source: "only one document supports this",
  page_level_citation_only: "citations point at a page rather than a line",
};

export const GATE_CHECK_LABELS: Record<string, string> = {
  EVIDENCE_EXISTS: "the cited evidence could not be found",
  BELONGS_TO_AUDIT: "the evidence belongs to a different audit",
  BELONGS_TO_DOCUMENT: "the citation does not match its document",
  LOCATION_VALID: "the cited location does not exist in that document",
  SUPPORTS_CLAIM: "the document no longer shows the recorded value",
  FRESH: "the evidence is older than this control allows",
  NO_CONTRADICTION: "the evidence contradicts itself",
  VALID_EVALUATION_METHOD: "this control was evaluated the wrong way",
  NO_INVENTED_FACTS: "a fact could not be traced to stored evidence",
  HUMAN_REVIEW_REQUIRED: "human review is required",
};

export const AUDIT_STATUS_LABELS: Record<AuditStatus, string> = {
  intake: "Intake",
  scoping: "Scoping",
  in_progress: "In progress",
  finalized: "Finalized",
};

export const ROLE_LABELS: Record<Role, string> = {
  auditor: "Auditor",
  reviewer: "Reviewer",
  admin: "Admin",
};
