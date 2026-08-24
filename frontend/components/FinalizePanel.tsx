"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { ApiError, api } from "@/lib/api";
import type {
  BlockingRequirement,
  EngagementDetail,
  FinalizationReadiness,
  FinalizeResult,
  Report,
} from "@/types/api";

interface Props {
  engagement: EngagementDetail;
  readiness: FinalizationReadiness;
  report: Report | null;
  /** True only for a Reviewer. The server checks the role again regardless. */
  canFinalize: boolean;
}

/**
 * Finalization — the single highest-stakes action in the product.
 *
 * Three rules from the specs shape this panel, and each is visible in the UI
 * rather than only in the API:
 *
 *  * **Reviewer only.** 00_PRODUCT.md §5.3: sign-off authority is a role
 *    property, not an escalation path — an Admin cannot do it either. The
 *    control is hidden for everyone else, but 04_API_CONTRACT.md requires the
 *    403 "even if an Auditor somehow gets a finalize button rendered
 *    client-side", so hiding it is courtesy and the server is the boundary.
 *
 *  * **Nothing unreviewed may pass.** Blockers are listed by clause with the
 *    reason, because a 409 that just says "unresolved findings" leaves the
 *    Reviewer hunting.
 *
 *  * **It must be a deliberate act.** 01_REQUIREMENTS.md forbids the system
 *    ever finalizing on a schedule or a batch, and calls this "the one action
 *    that must always be a deliberate human act". A single click on a terminal,
 *    irreversible action is not deliberate enough, so this asks for an explicit
 *    confirmation that states what is about to be signed.
 */
export function FinalizePanel({ engagement, readiness, report, canFinalize }: Props) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lateBlockers, setLateBlockers] = useState<BlockingRequirement[] | null>(null);

  const finalized = engagement.status === "finalized";

  async function finalize() {
    setBusy(true);
    setError(null);
    setLateBlockers(null);
    try {
      await api.post<FinalizeResult>(`/api/engagements/${engagement.id}/finalize`);
      setConfirming(false);
      startTransition(() => router.refresh());
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "UNRESOLVED_FINDINGS") {
          // The readiness snapshot this page rendered with can be stale — a
          // colleague may have uploaded evidence since. The server's list is
          // authoritative, so it replaces what is on screen.
          const blocking = caught.detail.blocking_requirements;
          setLateBlockers(Array.isArray(blocking) ? (blocking as BlockingRequirement[]) : []);
          setError("New work appeared since this page loaded. It is listed below.");
        } else if (caught.code === "ALREADY_FINALIZED") {
          setError("This engagement was already finalized.");
          startTransition(() => router.refresh());
        } else {
          setError(caught.displayMessage);
        }
      } else {
        setError("Could not reach the server. Nothing was finalized.");
      }
      setConfirming(false);
      setBusy(false);
    }
  }

  if (finalized) {
    return <FinalizedPanel engagement={engagement} report={report} />;
  }

  const blockers = lateBlockers ?? readiness.blocking_requirements;
  const ready = lateBlockers === null && readiness.ready;

  return (
    <section className="panel finalize">
      <div className="panel-head">
        <div>
          <h2>Sign off</h2>
          <p className="tiny muted">
            Finalizing produces the client report and closes the engagement. It cannot
            be undone.
          </p>
        </div>
      </div>

      <div className="panel-body stack-sm">
        {error && (
          <div className="note note-failed" role="alert">
            {error}
          </div>
        )}

        {blockers.length > 0 ? (
          <>
            <div className="note note-attention">
              <strong>
                {blockers.length} requirement{blockers.length === 1 ? "" : "s"}
              </strong>{" "}
              {blockers.length === 1 ? "is" : "are"} still blocking sign-off. Every
              confirmed requirement needs an approved finding, or an accepted gap.
            </div>

            <ul className="blocker-list">
              {blockers.map((blocker) => (
                <li key={blocker.scoped_requirement_id} className="blocker">
                  <span className="clause">{blocker.clause_id}</span>
                  <span className="small muted">{blocker.reason}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <div className="note note-ready">
            Everything in the confirmed scope has been resolved. This engagement is
            ready to sign off.
          </div>
        )}

        {!canFinalize ? (
          // Stated rather than silently omitted: an Auditor who has finished the
          // work should know why they cannot close it and who can.
          <p className="small muted">
            Only a Reviewer can sign off an engagement.
          </p>
        ) : confirming ? (
          <div className="finalize-confirm">
            <p className="small">
              <strong>Sign off {engagement.client_name}?</strong>
            </p>
            <ul className="small finalize-summary">
              <li>
                {engagement.counts.findings_approved} approved finding
                {engagement.counts.findings_approved === 1 ? "" : "s"} will be recorded
                in the report.
              </li>
              <li>
                {engagement.counts.findings_rejected} rejected finding
                {engagement.counts.findings_rejected === 1 ? "" : "s"} will be excluded
                but retained.
              </li>
              <li>
                Findings and evidence become read only. A correction after this needs a
                new, explicitly labelled record.
              </li>
              <li>Your name is recorded on the report as the signing Reviewer.</li>
            </ul>
            <div className="row">
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={finalize}
                disabled={busy}
              >
                {busy ? "Signing off…" : "Yes, sign off"}
              </button>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                onClick={() => setConfirming(false)}
                disabled={busy}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="row">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setConfirming(true)}
              disabled={!ready || busy}
              title={ready ? undefined : "Resolve the blocking requirements first"}
            >
              Finalize engagement
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

function FinalizedPanel({
  engagement,
  report,
}: {
  engagement: EngagementDetail;
  report: Report | null;
}) {
  const snapshot = report?.snapshot_data;

  return (
    <section className="panel finalize">
      <div className="panel-head">
        <div>
          <h2>Report</h2>
          <p className="tiny muted">
            {snapshot
              ? `Signed off by ${snapshot.generated_by.name}`
              : "This engagement is finalized."}
            {engagement.finalized_at && (
              <span className="mono"> · {engagement.finalized_at.slice(0, 10)}</span>
            )}
          </p>
        </div>
        <a
          className="btn btn-sm btn-primary"
          href={`/api/engagements/${engagement.id}/report?format=pdf`}
          download
        >
          Download PDF
        </a>
      </div>

      {snapshot && (
        <div className="panel-body stack-sm">
          <dl className="report-summary">
            <ReportStat
              label="Approved findings"
              value={snapshot.summary.approved_findings}
            />
            <ReportStat
              label="Accepted gaps"
              value={snapshot.summary.acknowledged_gaps}
            />
            <ReportStat
              label="Requirements in scope"
              value={snapshot.summary.confirmed_requirements}
            />
            <ReportStat label="Rejected, excluded" value={snapshot.rejected_finding_count} />
          </dl>

          <p className="tiny muted">
            Assessed against {snapshot.framework}, corpus{" "}
            <span className="mono">{snapshot.corpus_versions.join(", ")}</span>. Every
            finding in this report was approved by a named human assessor; AI
            suggestions are recorded alongside them for audit purposes and are not
            determinations.
          </p>

          {snapshot.acknowledged_gaps.length > 0 && (
            <div className="note note-attention">
              <strong className="tiny">
                {snapshot.acknowledged_gaps.length} accepted gap
                {snapshot.acknowledged_gaps.length === 1 ? "" : "s"}.
              </strong>{" "}
              <span className="small">
                These requirements were signed off without supporting evidence, with the
                reason recorded in the report.
              </span>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function ReportStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="report-stat">
      <dt className="tiny muted">{label}</dt>
      <dd className="mono report-stat-value">{value}</dd>
    </div>
  );
}
