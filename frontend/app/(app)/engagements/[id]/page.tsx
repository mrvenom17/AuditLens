import Link from "next/link";
import { notFound } from "next/navigation";

import { EngagementRail } from "@/components/EngagementRail";
import { ScopePanel } from "@/components/ScopePanel";
import { ApiError } from "@/lib/api";
import { serverFetch } from "@/lib/server-api";
import {
  ENGAGEMENT_STATUS_LABELS,
  type CurrentUser,
  type EngagementDetail,
  type ScopedRequirement,
} from "@/types/api";

import "./engagement.css";

export const metadata = { title: "Engagement · AuditLens" };

export default async function EngagementPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let engagement: EngagementDetail;
  try {
    engagement = await serverFetch<EngagementDetail>(`/api/engagements/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      notFound();
    }
    // A 403 is deliberately not converted to a 404 here. The API distinguishes
    // them on purpose for internal single-tenant software (ADR-011), and an
    // auditor who lands on a colleague's engagement is better served by "you
    // are not assigned to this" than by "this does not exist".
    throw error;
  }

  const [scope, user] = await Promise.all([
    serverFetch<ScopedRequirement[]>(`/api/engagements/${id}/scoped-requirements`),
    serverFetch<CurrentUser>("/api/auth/me"),
  ]);

  const finalized = engagement.status === "finalized";

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">
            <h1>{engagement.client_name}</h1>
            <span className={`pill ${finalized ? "pill-satisfied" : "pill-neutral"}`}>
              {ENGAGEMENT_STATUS_LABELS[engagement.status]}
            </span>
          </div>
          <p className="page-sub">
            PCI DSS v4.0.1 ·{" "}
            {engagement.entity_type === "merchant"
              ? `Merchant, level ${engagement.merchant_level}`
              : "Service provider"}
            {engagement.existing_saq_type ? ` · SAQ ${engagement.existing_saq_type}` : ""}
          </p>
        </div>
        <Link href="/engagements" className="btn btn-sm">
          All engagements
        </Link>
      </div>

      {finalized && (
        <div className="note note-attention finalized-banner">
          This engagement was finalized and is now read only. Findings and evidence
          cannot be changed; a correction requires a new, explicitly labelled record.
        </div>
      )}

      <div className="engagement-layout">
        <EngagementRail engagement={engagement} />

        <div className="stack">
          <ScopePanel
            engagementId={engagement.id}
            scope={scope}
            // Accepting a gap permits finalizing without evidence, so it carries
            // finalization-level authority and is Reviewer-only (ADR-012). The
            // server enforces this; hiding the control just avoids offering an
            // action that would be refused.
            canAcknowledgeGaps={user.role === "reviewer"}
            readOnly={finalized}
          />
        </div>
      </div>
    </>
  );
}
