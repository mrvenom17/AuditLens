"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { ApiError, api } from "@/lib/api";
import type { ScopeSuggestion, ScopedRequirement } from "@/types/api";

interface Props {
  engagementId: string;
  scope: ScopedRequirement[];
  /** Reviewer-only actions are hidden when false. The server re-checks. */
  canAcknowledgeGaps: boolean;
  readOnly: boolean;
}

/**
 * The scope table and its actions.
 *
 * This is the first screen where the LLM's absence is visible to the user, and
 * the shape of that moment matters. 01_REQUIREMENTS.md is explicit that a
 * failed suggestion is not an error: the endpoint returns 200 with
 * `manual_scoping_required`, and the auditor is expected to carry on by hand.
 * So the degraded state is presented as a route, not a failure — the manual
 * add-clause control is already on screen either way.
 */
export function ScopePanel({ engagementId, scope, canAcknowledgeGaps, readOnly }: Props) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [manualClause, setManualClause] = useState("");

  const confirmed = scope.filter((s) => s.confirmed);

  function refresh() {
    startTransition(() => router.refresh());
  }

  async function suggestScope() {
    setBusy("suggest");
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<ScopeSuggestion>(
        `/api/engagements/${engagementId}/scope-suggestion`,
      );
      if (result.manual_scoping_required) {
        // Not an error state. The tool is less automated right now, not broken,
        // and the message says what to do rather than what went wrong.
        setNotice(
          "The scope assistant is unavailable. Add the applicable clauses below and " +
            "confirm each one — the rest of the engagement works normally.",
        );
      } else {
        const saq = result.saq_type ? ` Suggested SAQ type: ${result.saq_type}.` : "";
        const ambiguous = result.ambiguous_entity_type
          ? " The entity type was ambiguous, so the broader scope was proposed — check it."
          : "";
        setNotice(
          `${result.proposed_requirements.length} requirement${
            result.proposed_requirements.length === 1 ? "" : "s"
          } proposed.${saq}${ambiguous} Nothing is in scope until you confirm it.`,
        );
      }
      refresh();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
    } finally {
      setBusy(null);
    }
  }

  async function addManual(event: React.FormEvent) {
    event.preventDefault();
    const clause = manualClause.trim();
    if (!clause) return;

    setBusy("add");
    setError(null);
    try {
      await api.post(`/api/engagements/${engagementId}/scoped-requirements`, {
        clause_id: clause,
      });
      setManualClause("");
      setNotice(`Clause ${clause} added to scope. Confirm it to make it count.`);
      refresh();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
    } finally {
      setBusy(null);
    }
  }

  async function setConfirmed(row: ScopedRequirement, confirmedNext: boolean) {
    setBusy(row.id);
    setError(null);
    setNotice(null);
    try {
      await api.patch(`/api/scoped-requirements/${row.id}`, { confirmed: confirmedNext });
      refresh();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
    } finally {
      setBusy(null);
    }
  }

  async function acknowledgeGap(row: ScopedRequirement) {
    const note = window.prompt(
      `Why is ${row.clause_id} being finalized without supporting evidence?\n\n` +
        "This is recorded in the report.",
    );
    if (note === null) return;
    if (!note.trim()) {
      setError("A gap needs a stated reason. Nothing was changed.");
      return;
    }

    setBusy(row.id);
    setError(null);
    try {
      await api.patch(`/api/scoped-requirements/${row.id}/gap`, {
        gap_acknowledged: true,
        gap_note: note,
      });
      refresh();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
    } finally {
      setBusy(null);
    }
  }

  const working = busy !== null || pending;

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Scope</h2>
          <p className="tiny muted">
            {confirmed.length} of {scope.length} confirmed
          </p>
        </div>
        {!readOnly && (
          <div className="row">
            <button
              type="button"
              className="btn btn-sm"
              onClick={suggestScope}
              disabled={working}
            >
              {busy === "suggest" ? "Asking…" : "Suggest scope"}
            </button>
          </div>
        )}
      </div>

      {(notice || error) && (
        <div className="scope-messages">
          {error && (
            <div className="note note-failed" role="alert">
              {error}
            </div>
          )}
          {notice && !error && (
            <div className="note" role="status">
              {notice}
            </div>
          )}
        </div>
      )}

      {scope.length === 0 ? (
        <div className="empty">
          <p>No requirements are in scope yet.</p>
          <p className="small" style={{ marginTop: "0.4rem" }}>
            Ask the assistant to propose a scope from the client profile, or add
            clauses by hand below.
          </p>
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th style={{ width: "5.5rem" }}>Clause</th>
              <th>Requirement</th>
              <th style={{ width: "6rem" }}>Source</th>
              <th style={{ width: "11rem" }}>State</th>
            </tr>
          </thead>
          <tbody>
            {scope.map((row) => (
              <tr key={row.id}>
                <td className="clause">{row.clause_id}</td>
                <td>
                  <div>{row.title}</div>
                  {row.rationale && (
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
                  {row.gap_acknowledged && row.gap_note && (
                    <div className="note note-attention scope-rationale">
                      <strong className="tiny">Accepted gap.</strong>{" "}
                      <span className="small">{row.gap_note}</span>
                    </div>
                  )}
                </td>
                <td className="small muted">
                  {row.source === "ai_suggested" ? "AI" : "Auditor"}
                </td>
                <td>
                  <div className="scope-actions">
                    {row.confirmed ? (
                      <span className="pill pill-satisfied">Confirmed</span>
                    ) : (
                      <span className="pill pill-neutral">Proposed</span>
                    )}

                    {!readOnly && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => setConfirmed(row, !row.confirmed)}
                        disabled={working}
                      >
                        {row.confirmed ? "Unconfirm" : "Confirm"}
                      </button>
                    )}

                    {!readOnly && canAcknowledgeGaps && row.confirmed && !row.gap_acknowledged && (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        onClick={() => acknowledgeGap(row)}
                        disabled={working}
                        title="Finalize this requirement without supporting evidence"
                      >
                        Accept gap
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!readOnly && (
        <form className="scope-add" onSubmit={addManual}>
          <label htmlFor="manual_clause" className="tiny">
            Add a clause by hand
          </label>
          <div className="row">
            <input
              id="manual_clause"
              className="mono scope-add-input"
              placeholder="1.2.1"
              value={manualClause}
              onChange={(e) => setManualClause(e.target.value)}
              disabled={working}
            />
            <button
              type="submit"
              className="btn btn-sm"
              disabled={working || !manualClause.trim()}
            >
              {busy === "add" ? "Adding…" : "Add"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
