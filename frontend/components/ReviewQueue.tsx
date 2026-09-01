"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { FindingCard } from "@/components/FindingCard";
import { ApiError, api } from "@/lib/api";
import type { Finding } from "@/types/api";

interface Props {
  auditId: string;
  findings: Finding[];
  canOverride: boolean;
  readOnly: boolean;
}

type Filter = "outstanding" | "all";

/**
 * The review queue — the product's centre of gravity.
 *
 * Defaults to outstanding work rather than everything, because the auditor's
 * actual task is "get through what is left". Decided findings stay reachable
 * because they are the audit record, never hidden permanently.
 */
export function ReviewQueue({ auditId, findings, canOverride, readOnly }: Props) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [filter, setFilter] = useState<Filter>("outstanding");
  const [rerunning, setRerunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const outstanding = findings.filter((f) => f.status === "pending_review");
  // Surfaced separately in the header: a result the gate could not verify is
  // the one an auditor should look at first, whatever else is queued
  // (01_REQUIREMENTS.md § Finding Review, Edge Cases).
  const unverified = findings.filter((f) => f.unverified_by_gate);
  const shown = filter === "outstanding" ? outstanding : findings;

  function refresh() {
    startTransition(() => router.refresh());
  }

  /**
   * Re-run the rule engine and Evidence Gate over the current facts.
   *
   * Appends new evaluations rather than editing the old ones, so the history of
   * what the engine concluded at each point survives. Useful after new evidence
   * lands, or after an Admin revises a control.
   */
  async function rerun() {
    setRerunning(true);
    setError(null);
    try {
      await api.post(`/api/audits/${auditId}/evaluate`, {});
      refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.displayMessage : "Could not re-run evaluation.",
      );
    } finally {
      setRerunning(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Findings</h2>
          <p className="tiny muted">
            {findings.length === 0
              ? "None yet"
              : `${outstanding.length} of ${findings.length} awaiting review`}
          </p>
          {unverified.length > 0 && (
            <p className="tiny">
              <span className="pill pill-failed">
                {unverified.length} could not be verified
              </span>
            </p>
          )}
        </div>

        <div className="row">
          {!readOnly && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={rerun}
              disabled={rerunning}
              title="Re-evaluate every scoped control against the current evidence"
            >
              {rerunning ? "Evaluating…" : "Re-run evaluation"}
            </button>
          )}
        </div>

        {findings.length > 0 && (
          <div className="row" role="group" aria-label="Filter findings">
            <button
              type="button"
              className={`btn btn-sm ${filter === "outstanding" ? "btn-primary" : ""}`}
              onClick={() => setFilter("outstanding")}
              aria-pressed={filter === "outstanding"}
            >
              Outstanding
            </button>
            <button
              type="button"
              className={`btn btn-sm ${filter === "all" ? "btn-primary" : ""}`}
              onClick={() => setFilter("all")}
              aria-pressed={filter === "all"}
            >
              All
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="note note-failed" role="alert">
          {error}
        </div>
      )}

      {findings.length === 0 ? (
        <div className="empty">
          <p>No findings yet.</p>
          <p className="small" style={{ marginTop: "0.4rem" }}>
            Upload evidence against the confirmed scope. AuditLens extracts the
            facts each control declares, runs them through that control&rsquo;s
            rules, and puts the result in front of you. Nothing is decided
            without you.
          </p>
        </div>
      ) : shown.length === 0 ? (
        <div className="empty">
          <p>Nothing outstanding.</p>
          <p className="small" style={{ marginTop: "0.4rem" }}>
            Every finding has been reviewed. Switch to All to see the record.
          </p>
        </div>
      ) : (
        <div className="queue">
          {shown.map((finding) => (
            <FindingCard
              key={finding.id}
              finding={finding}
              canOverride={canOverride}
              readOnly={readOnly}
              onReviewed={refresh}
            />
          ))}
        </div>
      )}
    </section>
  );
}
