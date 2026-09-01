"use client";

import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import {
  GATE_CHECK_LABELS,
  RESULT_LABELS,
  STRENGTH_FACTOR_LABELS,
  STRENGTH_LABELS,
  type EvaluationResult,
  type Finding,
  type FindingHistoryEntry,
} from "@/types/api";

interface Props {
  finding: Finding;
  /** Reviewers may change a finding someone already ruled on. Auditors may not. */
  canOverride: boolean;
  readOnly: boolean;
  onReviewed: () => void;
}

const RESULT_OPTIONS: EvaluationResult[] = [
  "PASS",
  "FAIL",
  "PARTIAL",
  "INSUFFICIENT_EVIDENCE",
  "CONFLICT",
  "NOT_APPLICABLE",
];

/**
 * One finding, as a complete decision unit (TASK-114).
 *
 * The card is laid out in the order an auditor actually decides: what the rule
 * required, what the evidence literally said, what the engine therefore
 * concluded, whether that conclusion could be verified — and only then the AI's
 * plain-English gloss, which is the least load-bearing thing on the screen.
 *
 * Three separations are structural here, not cosmetic:
 *
 * 1. **Engine vs AI.** The system result sits in `.engine`; the explanation sits
 *    in `.machine` with the reserved AI hue. They never share a treatment,
 *    because the product's whole claim is that they are different kinds of
 *    claim.
 * 2. **Machine vs human.** `system_result` and `auditor_decision` are rendered
 *    as separate rows and are never merged, matching the API contract that
 *    keeps them separate fields.
 * 3. **Verified vs not.** A gate-unverified result gets the `.unverified`
 *    treatment — the loudest in the stylesheet — because 01_REQUIREMENTS.md
 *    requires it be unmistakable from a normally-gated one.
 */
export function FindingCard({ finding, canOverride, readOnly, onReviewed }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"idle" | "override" | "reject" | "more">("idle");
  const [decision, setDecision] = useState<EvaluationResult>(finding.system_result);
  const [note, setNote] = useState("");
  const [history, setHistory] = useState<FindingHistoryEntry[] | null>(null);

  const decided = finding.status !== "pending_review";
  // A decided finding may only be changed by a Reviewer, and that change is
  // logged as an override rather than as a fresh decision.
  const actionable = !readOnly && (!decided || canOverride);

  async function submit(
    action: "approve" | "reject" | "request_more_evidence",
    auditorDecision?: EvaluationResult,
  ) {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/api/findings/${finding.id}/review`, {
        action,
        auditor_decision: auditorDecision ?? null,
        note: note.trim() || null,
      });
      setMode("idle");
      setNote("");
      onReviewed();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
      setBusy(false);
    }
  }

  async function loadHistory() {
    if (history) {
      setHistory(null);
      return;
    }
    try {
      setHistory(await api.get<FindingHistoryEntry[]>(`/api/findings/${finding.id}/history`));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Could not load history.");
    }
  }

  return (
    <article className={`finding ${decided ? "finding-decided" : ""}`}>
      <header className="finding-head">
        <span className="clause finding-clause">{finding.control_id}</span>

        <div className="row wrap grow">
          {decided ? (
            <span className={`pill pill-${finding.auditor_decision ?? "neutral"}`}>
              {finding.status === "rejected"
                ? "Rejected"
                : finding.status === "needs_more_evidence"
                  ? "More evidence requested"
                  : RESULT_LABELS[finding.auditor_decision ?? "NOT_APPLICABLE"]}
            </span>
          ) : (
            <span className="pill pill-neutral">Awaiting review</span>
          )}

          {finding.is_override && (
            <span className="pill pill-attention">Overrode the system result</span>
          )}
          {finding.unverified_by_gate && (
            <span className="pill pill-failed">Unverified</span>
          )}
          {finding.stale_evidence && (
            <span className="pill pill-attention">Stale evidence</span>
          )}
          <span
            className={`pill pill-strength-${finding.evidence_strength}`}
            title={
              finding.strength_factors
                .map((f) => STRENGTH_FACTOR_LABELS[f] ?? f)
                .join("; ") || undefined
            }
          >
            {STRENGTH_LABELS[finding.evidence_strength]} evidence
          </span>
        </div>

        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={loadHistory}
          aria-expanded={history !== null}
        >
          {history ? "Hide history" : "History"}
        </button>
      </header>

      {/* --- What is being tested. Before any verdict. ---------------------- */}
      <div className="requirement">
        <p className="small">
          <strong>{finding.control_name}</strong>
        </p>
        <p className="small">{finding.requirement_text}</p>
        {finding.assessment_procedures.length > 0 && (
          <details className="procedures">
            <summary className="tiny">
              Assessment procedure ({finding.assessment_procedures.length} steps)
            </summary>
            <ol className="tiny">
              {finding.assessment_procedures.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </details>
        )}
      </div>

      {/* --- What the engine determined, mechanically. --------------------- */}
      <div className="engine finding-machine">
        <div className="engine-label">
          <span aria-hidden="true">▪</span>
          System result · rule engine v{finding.engine_version}
          {!finding.llm_involved && " · no AI involved"}
        </div>

        <p className="small">
          <strong>{RESULT_LABELS[finding.system_result]}</strong>
          <span className="muted tiny"> · {finding.evaluation_mode.toLowerCase()}</span>
        </p>

        {finding.rules_used.length > 0 && (
          <div className="engine-rule">
            {finding.rules_used.map((rule, i) => (
              <div key={i}>
                {String(rule.fact)} {String(rule.operator)} {JSON.stringify(rule.expected)}
              </div>
            ))}
          </div>
        )}

        {finding.evidence_locations.length > 0 && (
          <p className="tiny finding-citations">
            Observed:{" "}
            {finding.evidence_locations.map((c, i) => (
              <span key={`${c.evidence_document_id}-${c.location}-${i}`}>
                {i > 0 && ", "}
                <span className="mono">
                  {c.fact} = {c.value}
                </span>{" "}
                {c.evidence_document_id ? (
                  <a
                    href={`/api/evidence-documents/${c.evidence_document_id}/download`}
                    download
                  >
                    ({c.location})
                  </a>
                ) : (
                  <>({c.location})</>
                )}
                {c.source_hash && (
                  <span className="muted" title={`SHA-256 ${c.source_hash}`}>
                    {" "}
                    sha256:{c.source_hash.slice(0, 8)}
                  </span>
                )}
              </span>
            ))}
          </p>
        )}

        {finding.strength_factors.length > 0 && (
          <p className="tiny">
            <strong>Evidence strength:</strong>{" "}
            {STRENGTH_LABELS[finding.evidence_strength].toLowerCase()} —{" "}
            {finding.strength_factors
              .map((f) => STRENGTH_FACTOR_LABELS[f] ?? f)
              .join("; ")}
            .
          </p>
        )}

        {finding.contradictions && finding.contradictions.length > 0 && (
          <p className="tiny">
            <strong>Evidence disagrees.</strong> Two or more documents give
            different values for the same setting; this has to be resolved by a
            person, not by the system.
          </p>
        )}
      </div>

      {/* --- Whether that result could be verified at all. ------------------ */}
      {finding.unverified_by_gate && (
        <div className="unverified finding-machine">
          <div className="unverified-label">
            <span aria-hidden="true">▲</span>
            Evidence gate: {finding.gate_status}
          </div>
          <p className="small">
            The system could not verify this evaluation. Assess this control
            manually — do not rely on the result above.
          </p>
          {finding.gate_checks_failed.length > 0 && (
            <ul className="gate-checks tiny">
              {finding.gate_checks_failed.map((check) => (
                <li key={check}>{GATE_CHECK_LABELS[check] ?? check}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* --- The AI's gloss. Never a determination. ------------------------- */}
      {finding.ai_explanation && (
        <div className={`machine finding-machine ${decided ? "machine-superseded" : ""}`}>
          <div className="machine-label">
            <span aria-hidden="true">◆</span>
            AI explanation · not a determination
          </div>
          {/* Rendered as text. This is model output derived from an untrusted
              document and goes through the same escaping path as any other
              content — never dangerouslySetInnerHTML (05_SECURITY.md §10.5). */}
          <p className="small">{finding.ai_explanation}</p>
        </div>
      )}

      {/* --- What a human decided. ------------------------------------------ */}
      {decided && (
        <div
          className={`determination determination-${finding.auditor_decision ?? "NOT_APPLICABLE"} finding-determination`}
        >
          <p className="small">
            <strong>
              {finding.status === "rejected"
                ? "Rejected by a reviewer"
                : finding.status === "needs_more_evidence"
                  ? "More evidence requested"
                  : `Auditor decided ${RESULT_LABELS[
                      finding.auditor_decision ?? "NOT_APPLICABLE"
                    ].toLowerCase()}`}
            </strong>
            {finding.reviewed_at && (
              <span className="muted mono tiny">
                {" "}
                · {finding.reviewed_at.slice(0, 16).replace("T", " ")}
              </span>
            )}
          </p>
          {finding.is_override && (
            <p className="tiny">
              This differs from the system result of{" "}
              <strong>{RESULT_LABELS[finding.system_result]}</strong>. Both are
              kept on the record.
            </p>
          )}
          {finding.review_note && <p className="small">{finding.review_note}</p>}
        </div>
      )}

      {history && (
        <ol className="finding-history">
          {history.map((entry) => (
            <li key={entry.id} className="tiny">
              <span className="mono">{entry.created_at.slice(0, 16).replace("T", " ")}</span>
              {" — "}
              <strong>{entry.action}</strong>
              {": "}
              {entry.previous_status} → {entry.new_status}
              {entry.new_decision && ` (${entry.new_decision})`}
              {entry.system_result && entry.new_decision !== entry.system_result && (
                <span className="muted"> · system said {entry.system_result}</span>
              )}
              {entry.note && <div className="muted">{entry.note}</div>}
            </li>
          ))}
        </ol>
      )}

      {error && (
        <div className="note note-failed finding-error" role="alert">
          {error}
        </div>
      )}

      {/* --- Actions -------------------------------------------------------- */}
      {actionable && (
        <div className="finding-actions">
          {mode === "idle" && (
            <>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => submit("approve")}
                disabled={busy}
              >
                {busy ? "Saving…" : `Agree — ${RESULT_LABELS[finding.system_result]}`}
              </button>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => {
                  setDecision(finding.system_result);
                  setMode("override");
                }}
                disabled={busy}
              >
                Record a different result
              </button>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setMode("more")}
                disabled={busy}
              >
                Request more evidence
              </button>
              <button
                type="button"
                className="btn btn-sm btn-danger"
                onClick={() => setMode("reject")}
                disabled={busy}
              >
                Reject
              </button>
              {decided && (
                <span className="tiny muted finding-override-hint">
                  Changing this is recorded as an override.
                </span>
              )}
            </>
          )}

          {mode === "override" && (
            <div className="finding-form">
              <div className="row wrap">
                <div>
                  <label htmlFor={`decision-${finding.id}`} className="tiny">
                    Your decision
                  </label>
                  <select
                    id={`decision-${finding.id}`}
                    value={decision}
                    onChange={(e) => setDecision(e.target.value as EvaluationResult)}
                    disabled={busy}
                  >
                    {RESULT_OPTIONS.map((r) => (
                      <option key={r} value={r}>
                        {RESULT_LABELS[r]}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label htmlFor={`note-${finding.id}`} className="tiny">
                  Why does this differ from the system result?
                </label>
                <textarea
                  id={`note-${finding.id}`}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  disabled={busy}
                  placeholder="Required when overriding. The system keeps both answers."
                />
              </div>
              <div className="row">
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => submit("approve", decision)}
                  // The server requires a note on an override; requiring it
                  // here too means the auditor is not told off after the fact.
                  disabled={busy || (decision !== finding.system_result && !note.trim())}
                >
                  {busy ? "Saving…" : "Save decision"}
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={() => setMode("idle")}
                  disabled={busy}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {(mode === "reject" || mode === "more") && (
            <div className="finding-form">
              <div>
                <label htmlFor={`reason-${finding.id}`} className="tiny">
                  {mode === "reject"
                    ? "Why is this being rejected?"
                    : "What further evidence is needed?"}
                </label>
                <textarea
                  id={`reason-${finding.id}`}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  disabled={busy}
                  placeholder="Required. This has to be explainable."
                />
              </div>
              <div className="row">
                <button
                  type="button"
                  className={`btn btn-sm ${mode === "reject" ? "btn-danger" : "btn-primary"}`}
                  onClick={() =>
                    submit(mode === "reject" ? "reject" : "request_more_evidence")
                  }
                  disabled={busy || !note.trim()}
                >
                  {busy
                    ? "Saving…"
                    : mode === "reject"
                      ? "Reject finding"
                      : "Request evidence"}
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={() => setMode("idle")}
                  disabled={busy}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {decided && !canOverride && !readOnly && (
        <p className="tiny muted finding-locked">
          Reviewed. Only a Reviewer can change it now.
        </p>
      )}
    </article>
  );
}
