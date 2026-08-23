import type { EngagementDetail } from "@/types/api";

/**
 * Engagement context rail.
 *
 * The numbers here are the ones that answer "how much is left" — the question
 * an auditor working a queue asks constantly. Two are surfaced as attention
 * states rather than plain counts, because 02_ARCHITECTURE.md §7.4 requires
 * needs_manual_review and extraction_failed be first-class UI states rather
 * than edge cases discovered by reading a table.
 */
export function EngagementRail({ engagement }: { engagement: EngagementDetail }) {
  const c = engagement.counts;

  const reviewed = c.findings_approved + c.findings_rejected;
  const progress = c.findings_total === 0 ? 0 : reviewed / c.findings_total;

  return (
    <aside className="rail">
      <section className="panel">
        <div className="panel-head">
          <h3>Review progress</h3>
        </div>
        <div className="panel-body">
          <div
            className="progress"
            role="progressbar"
            aria-valuenow={Math.round(progress * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Findings reviewed"
          >
            <div className="progress-fill" style={{ width: `${progress * 100}%` }} />
          </div>
          <p className="small muted rail-progress-label">
            {c.findings_total === 0
              ? "No findings yet"
              : `${reviewed} of ${c.findings_total} findings reviewed`}
          </p>

          {c.findings_draft > 0 && (
            <p className="small rail-blocking">
              {c.findings_draft} awaiting review
            </p>
          )}
        </div>
      </section>

      {c.findings_needing_manual_review > 0 && (
        <div className="note note-attention">
          <strong>{c.findings_needing_manual_review}</strong> finding
          {c.findings_needing_manual_review === 1 ? "" : "s"} need a closer look — the
          AI was unsure or unavailable.
        </div>
      )}

      <section className="panel">
        <div className="panel-head">
          <h3>Counts</h3>
        </div>
        <dl className="rail-stats">
          <Stat label="Requirements in scope" value={c.scoped_requirements} />
          <Stat label="Confirmed" value={c.confirmed_requirements} />
          <Stat label="Evidence requests" value={c.evidence_requests} />
          <Stat label="Documents" value={c.evidence_documents} />
          <Stat label="Findings approved" value={c.findings_approved} />
          <Stat label="Findings rejected" value={c.findings_rejected} />
        </dl>
      </section>

      {engagement.tech_stack_summary && (
        <section className="panel">
          <div className="panel-head">
            <h3>Client profile</h3>
          </div>
          <div className="panel-body">
            {/* Rendered as text, never as markup. This is firm-entered content
                and goes through the same escaping path as anything else
                (05_SECURITY.md §10.5). */}
            <p className="small">{engagement.tech_stack_summary}</p>
          </div>
        </section>
      )}
    </aside>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rail-stat">
      <dt className="small muted">{label}</dt>
      <dd className="mono rail-stat-value">{value}</dd>
    </div>
  );
}
