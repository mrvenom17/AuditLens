import Link from "next/link";

import { NewAuditForm } from "@/components/NewAuditForm";
import { serverFetch } from "@/lib/server-api";
import {
  AUDIT_STATUS_LABELS,
  type CurrentUser,
  type AuditSummary,
  type Page,
} from "@/types/api";

import "./audits.css";

export const metadata = { title: "Audits · AuditLens" };

export default async function AuditsPage() {
  const [page, user] = await Promise.all([
    serverFetch<Page<AuditSummary>>("/api/audits"),
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
          <h1>Audits</h1>
          <p className="page-sub">
            {user.role === "auditor"
              ? "Audits you are assigned to"
              : "All audits at the firm"}
          </p>
        </div>
        {canCreate && <NewAuditForm />}
      </div>

      <div className="panel">
        {page.items.length === 0 ? (
          <div className="empty">
            <p>
              {user.role === "auditor"
                ? "No audits are assigned to you."
                : "No audits yet."}
            </p>
            <p className="small" style={{ marginTop: "0.4rem" }}>
              {canCreate
                ? "Create one to begin scoping a client against PCI DSS v4.0.1."
                : "A Reviewer can assign you to an audit."}
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
              {page.items.map((audit) => (
                <tr key={audit.id}>
                  <td>
                    <Link href={`/audits/${audit.id}`} className="row-link">
                      {audit.client_name}
                    </Link>
                  </td>
                  <td className="small muted">
                    {audit.entity_type === "merchant"
                      ? `Merchant${audit.merchant_level ? ` · Level ${audit.merchant_level}` : ""}`
                      : "Service provider"}
                  </td>
                  <td>
                    <span
                      className={
                        audit.status === "finalized"
                          ? "pill pill-satisfied"
                          : "pill pill-neutral"
                      }
                    >
                      {AUDIT_STATUS_LABELS[audit.status]}
                    </span>
                  </td>
                  <td className="small muted mono">{audit.created_at.slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
