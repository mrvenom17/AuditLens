"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { ApiError, api } from "@/lib/api";
import type {
  EvidenceRequest,
  EvidenceRequestGenerateResult,
  EvidenceRequestStatus,
} from "@/types/api";

interface Props {
  auditId: string;
  requests: EvidenceRequest[];
  hasConfirmedScope: boolean;
  readOnly: boolean;
}

const STATUS_LABELS: Record<EvidenceRequestStatus, string> = {
  draft: "Draft",
  sent_externally: "Sent",
  received: "Received",
};

/**
 * The evidence checklist.
 *
 * ADR-004 is the rule that shapes this panel: the system drafts, the auditor
 * sends. Nothing here dispatches anything, and the copy says so plainly rather
 * than leaving someone to wonder whether clicking "Sent" mailed something.
 * Marking a request sent is the auditor's own note to self, and the interface
 * describes it that way.
 */
export function EvidenceRequestsPanel({
  auditId,
  requests,
  hasConfirmedScope,
  readOnly,
}: Props) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");

  const working = busy !== null || pending;

  function refresh() {
    startTransition(() => router.refresh());
  }

  async function generate() {
    setBusy("generate");
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<EvidenceRequestGenerateResult>(
        `/api/audits/${auditId}/evidence-requests/generate`,
      );

      const parts: string[] = [];
      if (result.created.length > 0) {
        parts.push(
          `${result.created.length} request${result.created.length === 1 ? "" : "s"} drafted.`,
        );
      }
      if (result.skipped_already_requested > 0) {
        parts.push(
          `${result.skipped_already_requested} requirement${
            result.skipped_already_requested === 1 ? " already had one" : "s already had one"
          }.`,
        );
      }
      if (!result.llm_available) {
        // The feature degraded but did not fail. Saying which is more useful
        // than a generic success, because the auditor will want to reword
        // template text before sending it.
        parts.push(
          "The drafting assistant was unavailable, so descriptions came from the clause text — reword them before sending.",
        );
      }
      setNotice(parts.join(" ") || "Nothing new to request.");
      refresh();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "NO_CONFIRMED_SCOPE") {
        setError(caught.message);
      } else {
        setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
      }
    } finally {
      setBusy(null);
    }
  }

  async function saveDescription(request: EvidenceRequest) {
    setBusy(request.id);
    setError(null);
    try {
      await api.patch(`/api/evidence-requests/${request.id}`, { description: draftText });
      setEditing(null);
      refresh();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
    } finally {
      setBusy(null);
    }
  }

  async function setStatus(request: EvidenceRequest, status: EvidenceRequestStatus) {
    setBusy(request.id);
    setError(null);
    try {
      await api.patch(`/api/evidence-requests/${request.id}`, { status });
      refresh();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Request failed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Evidence requests</h2>
          <p className="tiny muted">
            Drafted here, sent by you. AuditLens never contacts the client.
          </p>
        </div>
        {!readOnly && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={generate}
            disabled={working || !hasConfirmedScope}
            title={
              hasConfirmedScope
                ? undefined
                : "Confirm at least one requirement first"
            }
          >
            {busy === "generate" ? "Drafting…" : "Draft checklist"}
          </button>
        )}
      </div>

      {(error || notice) && (
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

      {requests.length === 0 ? (
        <div className="empty">
          <p>No evidence has been requested yet.</p>
          <p className="small" style={{ marginTop: "0.4rem" }}>
            {hasConfirmedScope
              ? "Draft a checklist from the confirmed scope, then send it to the client through your own channel."
              : "Confirm at least one requirement before drafting a checklist."}
          </p>
        </div>
      ) : (
        <ul className="request-list">
          {requests.map((request) => (
            <li key={request.id} className="request">
              <div className="request-head">
                <span className="clause">{request.control_id}</span>
                <span className={`pill ${statusPill(request.status)}`}>
                  {STATUS_LABELS[request.status]}
                </span>
                {request.description_source === "template" && (
                  <span className="pill pill-attention" title="Drafted from clause text, not by the assistant">
                    Template wording
                  </span>
                )}
              </div>

              {editing === request.id ? (
                <div className="request-edit">
                  <textarea
                    value={draftText}
                    onChange={(e) => setDraftText(e.target.value)}
                    maxLength={5000}
                    disabled={working}
                    aria-label={`Description for clause ${request.control_id}`}
                  />
                  <div className="row">
                    <button
                      type="button"
                      className="btn btn-sm btn-primary"
                      onClick={() => saveDescription(request)}
                      disabled={working || !draftText.trim()}
                    >
                      Save
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={() => setEditing(null)}
                      disabled={working}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <p className="request-text small">{request.description}</p>
              )}

              {!readOnly && editing !== request.id && (
                <div className="request-actions">
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost"
                    onClick={() => {
                      setEditing(request.id);
                      setDraftText(request.description);
                    }}
                    disabled={working}
                  >
                    Reword
                  </button>
                  {request.status === "draft" && (
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={() => setStatus(request, "sent_externally")}
                      disabled={working}
                      title="Records that you sent this. AuditLens does not send it for you."
                    >
                      Mark as sent
                    </button>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function statusPill(status: EvidenceRequestStatus): string {
  if (status === "received") return "pill-satisfied";
  if (status === "sent_externally") return "pill-neutral";
  return "pill-neutral";
}
