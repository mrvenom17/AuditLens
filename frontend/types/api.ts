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

export type EngagementStatus = "intake" | "scoping" | "in_progress" | "finalized";

export type ScopeSource = "ai_suggested" | "manual";

export type EvidenceRequestStatus = "draft" | "sent_externally" | "received";

export type ExtractionStatus = "processing" | "complete" | "extraction_failed";

export type FindingStatus = "draft" | "approved" | "rejected";

/** The four values both the AI may suggest and a human may determine. */
export type ComplianceStatus =
  | "satisfied"
  | "partial"
  | "not_satisfied"
  | "not_applicable";

export type FindingAction = "accept" | "edit" | "reject" | "override";

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

/* --- Engagements ------------------------------------------------------------ */

/** Mirrors EngagementSummary. Omits tech_stack_summary, which is Sensitive. */
export interface EngagementSummary {
  id: string;
  client_name: string;
  entity_type: EntityType;
  merchant_level: MerchantLevel | null;
  status: EngagementStatus;
  created_at: string;
  updated_at: string;
}

/** Mirrors EngagementCounts. */
export interface EngagementCounts {
  scoped_requirements: number;
  confirmed_requirements: number;
  evidence_requests: number;
  evidence_documents: number;
  findings_total: number;
  findings_draft: number;
  findings_approved: number;
  findings_rejected: number;
  findings_needing_manual_review: number;
}

/** Mirrors EngagementDetail. */
export interface EngagementDetail {
  id: string;
  client_name: string;
  entity_type: EntityType;
  merchant_level: MerchantLevel | null;
  annual_transaction_volume: number | null;
  existing_saq_type: string | null;
  tech_stack_summary: string | null;
  status: EngagementStatus;
  created_by: string;
  finalized_by: string | null;
  finalized_at: string | null;
  created_at: string;
  updated_at: string;
  counts: EngagementCounts;
  assigned_user_ids: string[];
}

/** Mirrors EngagementCreate. */
export interface EngagementCreate {
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
export interface ScopedRequirement {
  id: string;
  engagement_id: string;
  pci_requirement_id: string;
  clause_id: string;
  title: string;
  requirement_family: number;
  source: ScopeSource;
  confirmed: boolean;
  rationale: string | null;
  gap_acknowledged: boolean;
  gap_note: string | null;
  created_at: string;
  updated_at: string;
}

/** Mirrors ScopeSuggestionResponse. */
export interface ScopeSuggestion {
  proposed_requirements: ScopedRequirement[];
  /** True when the LLM was unavailable. A degraded success, never an error. */
  manual_scoping_required: boolean;
  saq_type: string | null;
  ambiguous_entity_type: boolean;
}

/* --- Evidence --------------------------------------------------------------- */

/** Mirrors EvidenceRequestResponse. */
export interface EvidenceRequest {
  id: string;
  engagement_id: string;
  scoped_requirement_id: string;
  clause_id: string;
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
  engagement_id: string;
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

/** Mirrors Citation. */
export interface Citation {
  evidence_document_id: string;
  location: string;
}

/**
 * Mirrors FindingResponse.
 *
 * The AI fields and the human fields are deliberately separate and are never
 * merged — 04_API_CONTRACT.md requires the response schema itself make it
 * impossible to mistake a draft suggestion for a final determination. The UI
 * carries that distinction through to the pixel; see `.machine` in globals.css.
 */
export interface Finding {
  id: string;
  engagement_id: string;
  scoped_requirement_id: string;
  clause_id: string;

  /** What the machine suggested. Null when the LLM call failed. */
  ai_suggested_status: ComplianceStatus | null;
  ai_confidence: number | null;
  ai_rationale: string | null;
  needs_manual_review: boolean;

  /** What a human determined. Null until approved. */
  status: FindingStatus;
  final_status: ComplianceStatus | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;

  citations: Citation[];
  evidence_document_ids: string[];
  created_at: string;
  updated_at: string;

  /** Server-computed. True while no human has ruled on this finding. */
  is_ai_draft: boolean;
}

/** Mirrors FindingReviewRequest. Deliberately has no reviewed_by field. */
export interface FindingReviewRequest {
  action: Exclude<FindingAction, "override">;
  edited_status?: ComplianceStatus | null;
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
  previous_final_status: ComplianceStatus | null;
  new_final_status: ComplianceStatus | null;
  note: string | null;
  created_at: string;
}

/* --- Finalization ----------------------------------------------------------- */

/** Mirrors BlockingRequirement. */
export interface BlockingRequirement {
  scoped_requirement_id: string;
  clause_id: string;
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
  engagement_status: string;
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
  clause_id: string;
  requirement_family: number;
  title: string;
  requirement_text: string;
  final_status: ComplianceStatus | null;
  review_note: string | null;
  ai_suggested_status: ComplianceStatus | null;
  ai_confidence: number | null;
  ai_rationale: string | null;
  citations: Citation[];
  reviewed_by: string;
  reviewed_at: string | null;
}

export interface ReportSnapshotGap {
  clause_id: string;
  title: string;
  gap_note: string | null;
}

export interface ReportSnapshot {
  engagement: {
    id: string;
    client_name: string;
    entity_type: EntityType;
    merchant_level: MerchantLevel | null;
    existing_saq_type: string | null;
  };
  framework: string;
  corpus_versions: string[];
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
  engagement_id: string;
  generated_by: string;
  generated_at: string;
  snapshot_data: ReportSnapshot;
}

/* --- Display helpers -------------------------------------------------------- */

export const COMPLIANCE_STATUS_LABELS: Record<ComplianceStatus, string> = {
  satisfied: "Satisfied",
  partial: "Partially satisfied",
  not_satisfied: "Not satisfied",
  not_applicable: "Not applicable",
};

export const ENGAGEMENT_STATUS_LABELS: Record<EngagementStatus, string> = {
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
