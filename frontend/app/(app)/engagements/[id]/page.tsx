import Link from "next/link";
import { notFound } from "next/navigation";

import { EngagementRail } from "@/components/EngagementRail";
import { ApiError } from "@/lib/api";
import { serverFetch } from "@/lib/server-api";
import {
  ENGAGEMENT_STATUS_LABELS,
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

  const scope = await serverFetch<ScopedRequirement[]>(
    `/api/engagements/${id}/scoped-requirements`,
  );
  const confirmed = scope.filter((s) => s.confirmed);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">
            <h1>{engagement.client_name}</h1>
            <span className={`pill ${statusPill(engagement.status)}`}>
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

      {engagement.status === "finalized" && (
        <div className="note note-attention finalized-banner">
          This engagement was finalized and is now read only. Findings and evidence
          cannot be changed; a correction requires a new, explicitly labelled record.
        </div>
      )}

      <div className="engagement-layout">
        <EngagementRail engagement={engagement} />

        <div className="stack">
          <section className="panel">
            <div className="panel-head">
              <h2>Scope</h2>
              <span className="small muted">
                {confirmed.length} of {scope.length} confirmed
              </span>
            </div>

            {scope.length === 0 ? (
              <div className="empty">
                <p>No requirements are in scope yet.</p>
                <p className="small" style={{ marginTop: "0.4rem" }}>
                  Suggest a scope from the client profile, or add clauses by hand.
                </p>
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th style={{ width: "5.5rem" }}>Clause</th>
                    <th>Requirement</th>
                    <th style={{ width: "7rem" }}>Source</th>
                    <th style={{ width: "7rem" }}>State</th>
                  </tr>
                </thead>
                <tbody>
                  {scope.map((row) => (
                    <tr key={row.id}>
                      <td className="clause">{row.clause_id}</td>
                      <td>
                        <div>{row.title}</div>
                        {row.rationale && (
                          // Rationale on an ai_suggested row is machine-authored,
                          // so it carries the reserved provenance treatment. On a
                          // manual row it is the auditor's own note and does not.
                          <div
                            className={
                              row.source === "ai_suggested"
                                ? "machine scope-rationale"
                                : "note scope-rationale"
                            }
                          >
                            {row.source === "ai_suggested" && (
                              <div className="machine-label">AI suggested</div>
                            )}
                            <p className="small">{row.rationale}</p>
                          </div>
                        )}
                      </td>
                      <td className="small muted">
                        {row.source === "ai_suggested" ? "AI" : "Auditor"}
                      </td>
                      <td>
                        {row.confirmed ? (
                          <span className="pill pill-satisfied">Confirmed</span>
                        ) : (
                          <span className="pill pill-neutral">Proposed</span>
                        )}
                        {row.gap_acknowledged && (
                          <div style={{ marginTop: "0.3rem" }}>
                            <span className="pill pill-attention">Gap accepted</span>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      </div>
    </>
  );
}

function statusPill(status: EngagementDetail["status"]): string {
  return status === "finalized" ? "pill-satisfied" : "pill-neutral";
}
