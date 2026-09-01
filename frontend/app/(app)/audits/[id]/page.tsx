import Link from "next/link";
import { notFound } from "next/navigation";

import { AuditRail } from "@/components/AuditRail";
import { EvidencePanel } from "@/components/EvidencePanel";
import { EvidenceRequestsPanel } from "@/components/EvidenceRequestsPanel";
import { FinalizePanel } from "@/components/FinalizePanel";
import { ReviewQueue } from "@/components/ReviewQueue";
import { ScopePanel } from "@/components/ScopePanel";
import { ApiError } from "@/lib/api";
import { serverFetch, serverFetchOrNull } from "@/lib/server-api";
import {
  AUDIT_STATUS_LABELS,
  type CurrentUser,
  type AuditDetail,
  type EvidenceDocumentSummary,
  type EvidenceRequest,
  type FinalizationReadiness,
  type Finding,
  type Report,
  type ScopedControl,
} from "@/types/api";

import "./audit.css";

export const metadata = { title: "Audit · AuditLens" };

export default async function AuditPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let audit: AuditDetail;
  try {
    audit = await serverFetch<AuditDetail>(`/api/audits/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      notFound();
    }
    // A 403 is deliberately not converted to a 404 here. The API distinguishes
    // them on purpose for internal single-tenant software (ADR-011), and an
    // auditor who lands on a colleague's audit is better served by "you
    // are not assigned to this" than by "this does not exist".
    throw error;
  }

  const [scope, requests, documents, findings, readiness, report, user] =
    await Promise.all([
      serverFetch<ScopedControl[]>(`/api/audits/${id}/scoped-requirements`),
      serverFetch<EvidenceRequest[]>(`/api/audits/${id}/evidence-requests`),
      serverFetch<EvidenceDocumentSummary[]>(`/api/audits/${id}/evidence-documents`),
      serverFetch<Finding[]>(`/api/audits/${id}/findings`),
      serverFetch<FinalizationReadiness>(
        `/api/audits/${id}/finalization-readiness`,
      ),
      // 404 until the audit is finalized, which is the normal case rather
      // than a fault — so a missing report is null, not an error.
      serverFetchOrNull<Report>(`/api/audits/${id}/report`),
      serverFetch<CurrentUser>("/api/auth/me"),
    ]);

  const finalized = audit.status === "finalized";
  const hasConfirmedScope = scope.some((s) => s.confirmed);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">
            <h1>{audit.client_name}</h1>
            <span className={`pill ${finalized ? "pill-satisfied" : "pill-neutral"}`}>
              {AUDIT_STATUS_LABELS[audit.status]}
            </span>
          </div>
          <p className="page-sub">
            PCI DSS v4.0.1 ·{" "}
            {audit.entity_type === "merchant"
              ? `Merchant, level ${audit.merchant_level}`
              : "Service provider"}
            {audit.existing_saq_type ? ` · SAQ ${audit.existing_saq_type}` : ""}
          </p>
        </div>
        <Link href="/audits" className="btn btn-sm">
          All audits
        </Link>
      </div>

      {finalized && (
        <div className="note note-attention finalized-banner">
          This audit was finalized and is now read only. Findings and evidence
          cannot be changed; a correction requires a new, explicitly labelled record.
        </div>
      )}

      <div className="audit-layout">
        <AuditRail audit={audit} />

        <div className="stack">
          <ScopePanel
            auditId={audit.id}
            scope={scope}
            // Accepting a gap permits finalizing without evidence, so it carries
            // finalization-level authority and is Reviewer-only (ADR-012). The
            // server enforces this; hiding the control just avoids offering an
            // action that would be refused.
            canAcknowledgeGaps={user.role === "reviewer"}
            readOnly={finalized}
          />

          <ReviewQueue
            auditId={id}
            findings={findings}
            // Only a Reviewer may change a finding someone already ruled on
            // (01_REQUIREMENTS.md § Finding Review, Authorization Rules).
            canOverride={user.role === "reviewer"}
            readOnly={finalized}
          />

          <FinalizePanel
            audit={audit}
            readiness={readiness}
            report={report}
            // 00_PRODUCT.md §5.3: sign-off authority is a role property, not an
            // escalation path — an Admin is excluded too. The server enforces
            // this regardless of what is rendered.
            canFinalize={user.role === "reviewer"}
          />

          <EvidenceRequestsPanel
            auditId={audit.id}
            requests={requests}
            hasConfirmedScope={hasConfirmedScope}
            readOnly={finalized}
          />

          <EvidencePanel
            auditId={audit.id}
            documents={documents}
            requests={requests}
            readOnly={finalized}
          />
        </div>
      </div>
    </>
  );
}
