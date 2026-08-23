import Link from "next/link";

import { NewEngagementForm } from "@/components/NewEngagementForm";
import { serverFetch } from "@/lib/server-api";
import {
  ENGAGEMENT_STATUS_LABELS,
  type CurrentUser,
  type EngagementSummary,
  type Page,
} from "@/types/api";

import "./engagements.css";

export const metadata = { title: "Engagements · AuditLens" };

export default async function EngagementsPage() {
  const [page, user] = await Promise.all([
    serverFetch<Page<EngagementSummary>>("/api/engagements"),
    serverFetch<CurrentUser>("/api/auth/me"),
  ]);

  // 04_API_CONTRACT.md restricts creation to auditor and reviewer. Hiding the
  // control for an Admin is a courtesy — the endpoint refuses them regardless,
  // and that refusal is the actual boundary (02_ARCHITECTURE.md §7.4).
  const canCreate = user.role === "auditor" || user.role === "reviewer";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Engagements</h1>
          <p className="page-sub">
            {user.role === "auditor"
              ? "Engagements you are assigned to"
              : "All engagements at the firm"}
          </p>
        </div>
        {canCreate && <NewEngagementForm />}
      </div>

      <div className="panel">
        {page.items.length === 0 ? (
          <div className="empty">
            <p>
              {user.role === "auditor"
                ? "No engagements are assigned to you."
                : "No engagements yet."}
            </p>
            <p className="small" style={{ marginTop: "0.4rem" }}>
              {canCreate
                ? "Create one to begin scoping a client against PCI DSS v4.0.1."
                : "A Reviewer can assign you to an engagement."}
            </p>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Client</th>
                <th style={{ width: "12rem" }}>Type</th>
                <th style={{ width: "8rem" }}>Status</th>
                <th style={{ width: "7rem" }}>Opened</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((engagement) => (
                <tr key={engagement.id}>
                  <td>
                    <Link href={`/engagements/${engagement.id}`} className="row-link">
                      {engagement.client_name}
                    </Link>
                  </td>
                  <td className="small muted">
                    {engagement.entity_type === "merchant"
                      ? `Merchant${engagement.merchant_level ? ` · Level ${engagement.merchant_level}` : ""}`
                      : "Service provider"}
                  </td>
                  <td>
                    <span
                      className={
                        engagement.status === "finalized"
                          ? "pill pill-satisfied"
                          : "pill pill-neutral"
                      }
                    >
                      {ENGAGEMENT_STATUS_LABELS[engagement.status]}
                    </span>
                  </td>
                  <td className="small muted mono">{engagement.created_at.slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
