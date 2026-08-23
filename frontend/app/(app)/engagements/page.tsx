import { serverFetch } from "@/lib/server-api";
import {
  ENGAGEMENT_STATUS_LABELS,
  type EngagementSummary,
  type Page,
} from "@/types/api";

export const metadata = { title: "Engagements · AuditLens" };

export default async function EngagementsPage() {
  const page = await serverFetch<Page<EngagementSummary>>("/api/engagements");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Engagements</h1>
          <p className="page-sub">
            {page.total === 0
              ? "No engagements yet"
              : `${page.total} engagement${page.total === 1 ? "" : "s"}`}
          </p>
        </div>
      </div>

      <div className="panel">
        {page.items.length === 0 ? (
          <div className="empty">
            {/* An empty screen is an invitation to act, not a shrug. */}
            <p>No engagements are assigned to you.</p>
            <p className="small" style={{ marginTop: "0.4rem" }}>
              Create one to begin scoping a client against PCI DSS v4.0.1.
            </p>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Client</th>
                <th>Type</th>
                <th>Status</th>
                <th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((engagement) => (
                <tr key={engagement.id}>
                  <td>
                    <span style={{ fontWeight: 500 }}>{engagement.client_name}</span>
                  </td>
                  <td className="small muted">
                    {engagement.entity_type === "merchant"
                      ? `Merchant${engagement.merchant_level ? ` · Level ${engagement.merchant_level}` : ""}`
                      : "Service provider"}
                  </td>
                  <td>
                    <span className="pill pill-neutral">
                      {ENGAGEMENT_STATUS_LABELS[engagement.status]}
                    </span>
                  </td>
                  <td className="small muted mono">
                    {engagement.created_at.slice(0, 10)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
