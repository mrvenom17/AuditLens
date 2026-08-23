"use client";

import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import {
  COMPLIANCE_STATUS_LABELS,
  type ComplianceStatus,
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

const STATUS_OPTIONS: ComplianceStatus[] = [
  "satisfied",
  "partial",
  "not_satisfied",
  "not_applicable",
];

/**
 * One finding, as a complete decision unit.
 *
 * The auditor's job here is "decide this one, move on", so everything needed
 * for that decision is in the card: the clause, what the machine said, what a
 * human already said if anyone has, and the actions.
 *
 * The provenance rule does its real work here. The AI block carries the
 * reserved hue and a mono attribution; the human determination carries a status
 * rule. When a human has ruled, the AI block dims but never disappears —
 * 01_REQUIREMENTS.md requires the original suggestion be retained even when
 * overridden, and hiding it would quietly break the audit trail the product
 * exists to produce.
 */
export function FindingCard({ finding, canOverride, readOnly, onReviewed }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"idle" | "edit" | "reject">("idle");
  const [editedStatus, setEditedStatus] = useState<ComplianceStatus>(
    finding.ai_suggested_status ?? "partial",
  );
  const [note, setNote] = useState("");
  const [history, setHistory] = useState<FindingHistoryEntry[] | null>(null);

  const decided = finding.status !== "draft";
  // A decided finding may only be changed by a Reviewer, and 01_REQUIREMENTS.md
  // logs that change as an override rather than as a fresh decision.
  const actionable = !readOnly && (!decided || canOverride);

  async function submit(action: "accept" | "edit" | "reject") {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/api/findings/${finding.id}/review`, {
        action,
        edited_status: action === "edit" ? editedStatus : null,
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
      setHistory(
        await api.get<FindingHistoryEntry[]>(`/api/findings/${finding.id}/history`),
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Could not load history.");
    }
  }

  return (
    <article className={`finding ${decided ? "finding-decided" : ""}`}>
      <header className="finding-head">
        <span className="clause finding-clause">{finding.clause_id}</span>

        <div className="row wrap grow">
          {decided ? (
            <span
              className={`pill pill-${finding.final_status ?? "not_applicable"}`}
            >
              {finding.status === "rejected"
                ? "Rejected"
                : COMPLIANCE_STATUS_LABELS[finding.final_status ?? "not_applicable"]}
            </span>
          ) : (
            <span className="pill pill-neutral">Awaiting review</span>
          )}

          {finding.needs_manual_review && !decided && (
            <span className="pill pill-attention">Needs a closer look</span>
          )}
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

      {/* --- What the machine said. Never a determination. ------------------ */}
      <div className={`machine finding-machine ${decided ? "machine-superseded" : ""}`}>
        <div className="machine-label">
          <span aria-hidden="true">◆</span>
          AI draft{decided ? " · superseded" : ""}
        </div>

        {finding.ai_suggested_status === null ? (
          <p className="small">
            No suggestion available — the analysis service could not assess this
            evidence. Set the status yourself using Edit.
          </p>
        ) : (
          <>
            <p className="small finding-machine-status">
              Suggested <strong>{COMPLIANCE_STATUS_LABELS[finding.ai_suggested_status]}</strong>
              {finding.ai_confidence !== null && (
                <>
                  {" "}
                  at{" "}
                  <span className="mono">
                    {(finding.ai_confidence * 100).toFixed(0)}%
                  </span>{" "}
                  confidence
                </>
              )}
            </p>
            {finding.ai_rationale && (
              // Rendered as text. This is model output derived from an
              // untrusted document and goes through the same escaping path as
              // any other content — never dangerouslySetInnerHTML
              // (05_SECURITY.md §10.5).
              <p className="small">{finding.ai_rationale}</p>
            )}
          </>
        )}

        {finding.citations.length > 0 && (
          <p className="tiny finding-citations">
            Cited:{" "}
            {finding.citations.map((c, i) => (
              <span key={`${c.evidence_document_id}-${c.location}`}>
                {i > 0 && ", "}
                <a href={`/api/evidence-documents/${c.evidence_document_id}/download`} download>
                  {c.location}
                </a>
              </span>
            ))}
          </p>
        )}
      </div>

      {/* --- What a human determined. ---------------------------------------- */}
      {decided && (
        <div
          className={`determination determination-${finding.final_status ?? "not_applicable"} finding-determination`}
        >
          <p className="small">
            <strong>
              {finding.status === "rejected"
                ? "Rejected by a reviewer"
                : `Determined ${COMPLIANCE_STATUS_LABELS[finding.final_status ?? "not_applicable"].toLowerCase()}`}
            </strong>
            {finding.reviewed_at && (
              <span className="muted mono tiny">
                {" "}
                · {finding.reviewed_at.slice(0, 16).replace("T", " ")}
              </span>
            )}
          </p>
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
              {entry.new_final_status && ` (${entry.new_final_status})`}
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

      {/* --- Actions ---------------------------------------------------------- */}
      {actionable && (
        <div className="finding-actions">
          {mode === "idle" && (
            <>
              {finding.ai_suggested_status !== null && (
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => submit("accept")}
                  disabled={busy}
                >
                  {busy ? "Saving…" : decided ? "Accept AI draft" : "Accept"}
                </button>
              )}
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setMode("edit")}
                disabled={busy}
              >
                {finding.ai_suggested_status === null ? "Set status" : "Edit"}
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

          {mode === "edit" && (
            <div className="finding-form">
              <div className="row wrap">
                <div>
                  <label htmlFor={`status-${finding.id}`} className="tiny">
                    Determination
                  </label>
                  <select
                    id={`status-${finding.id}`}
                    value={editedStatus}
                    onChange={(e) => setEditedStatus(e.target.value as ComplianceStatus)}
                    disabled={busy}
                  >
                    {STATUS_OPTIONS.map((s) => (
                      <option key={s} value={s}>
                        {COMPLIANCE_STATUS_LABELS[s]}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label htmlFor={`note-${finding.id}`} className="tiny">
                  Note (optional)
                </label>
                <textarea
                  id={`note-${finding.id}`}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  disabled={busy}
                  placeholder="What in the evidence supports this?"
                />
              </div>
              <div className="row">
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => submit("edit")}
                  disabled={busy}
                >
                  {busy ? "Saving…" : "Save determination"}
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

          {mode === "reject" && (
            <div className="finding-form">
              <div>
                <label htmlFor={`reject-${finding.id}`} className="tiny">
                  Why is this being rejected?
                </label>
                <textarea
                  id={`reject-${finding.id}`}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  disabled={busy}
                  placeholder="Required. A rejection has to be explainable."
                />
              </div>
              <div className="row">
                <button
                  type="button"
                  className="btn btn-sm btn-danger"
                  onClick={() => submit("reject")}
                  // The server requires a note on rejection; requiring it here
                  // too means the auditor is not told off after the fact.
                  disabled={busy || !note.trim()}
                >
                  {busy ? "Saving…" : "Reject finding"}
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
