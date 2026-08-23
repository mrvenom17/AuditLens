"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { FindingCard } from "@/components/FindingCard";
import type { Finding } from "@/types/api";

interface Props {
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
export function ReviewQueue({ findings, canOverride, readOnly }: Props) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [filter, setFilter] = useState<Filter>("outstanding");

  const outstanding = findings.filter((f) => f.status === "draft");
  const shown = filter === "outstanding" ? outstanding : findings;

  function refresh() {
    startTransition(() => router.refresh());
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

      {findings.length === 0 ? (
        <div className="empty">
          <p>No findings yet.</p>
          <p className="small" style={{ marginTop: "0.4rem" }}>
            Upload evidence against the confirmed scope. AuditLens drafts a finding for
            each clause the evidence appears to address, and every one waits for you.
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
