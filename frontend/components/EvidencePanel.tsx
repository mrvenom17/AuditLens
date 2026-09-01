"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";

import { ApiError, api } from "@/lib/api";
import type {
  EvidenceDocumentSummary,
  EvidenceRequest,
  ExtractionStatus,
} from "@/types/api";

interface Props {
  auditId: string;
  documents: EvidenceDocumentSummary[];
  requests: EvidenceRequest[];
  readOnly: boolean;
}

/** Mirrors the server's allow-list (05_SECURITY.md §10.4). Used only to set the
 *  file picker's filter — the server decides by inspecting content, and a
 *  renamed file is refused there regardless of what this says. */
const ACCEPT = ".pdf,.docx,.xlsx,.png,.jpg,.jpeg";
const MAX_MB = 25;

export function EvidencePanel({ auditId, documents, requests, readOnly }: Props) {
  const router = useRouter();
  const [refreshing, startTransition] = useTransition();
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkTo, setLinkTo] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  // 02_ARCHITECTURE.md §7.5: extraction runs in a background worker, so a
  // freshly uploaded document arrives as `processing` and changes underneath
  // the page. Polling while anything is in flight is what makes that visible
  // without the auditor learning to press refresh.
  const inFlight = documents.some(
    (d) => d.extraction_status === "processing" || d.matching_status === "in_progress",
  );

  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(() => startTransition(() => router.refresh()), 4000);
    return () => clearInterval(timer);
  }, [inFlight, router]);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    const file = fileInput.current?.files?.[0];
    if (!file) return;

    // A local size check so a 40MB file is refused instantly instead of after
    // a long upload. The server enforces the real limit while streaming.
    if (file.size > MAX_MB * 1024 * 1024) {
      setError(`That file is larger than ${MAX_MB}MB. The client will need to split it.`);
      return;
    }

    setUploading(true);
    setError(null);

    const form = new FormData();
    form.append("file", file);
    if (linkTo) form.append("evidence_request_id", linkTo);

    try {
      await api.upload(`/api/audits/${auditId}/evidence-documents`, form);
      if (fileInput.current) fileInput.current.value = "";
      setLinkTo("");
      startTransition(() => router.refresh());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.displayMessage : "Upload failed.");
    } finally {
      setUploading(false);
    }
  }

  const openRequests = requests.filter((r) => r.status !== "received");

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Evidence</h2>
          <p className="tiny muted">
            {documents.length === 0
              ? "Nothing uploaded yet"
              : `${documents.length} document${documents.length === 1 ? "" : "s"}`}
          </p>
        </div>
        {inFlight && <span className="pill pill-neutral">Processing…</span>}
      </div>

      {error && (
        <div className="scope-messages">
          <div className="note note-failed" role="alert">
            {error}
          </div>
        </div>
      )}

      {documents.length === 0 ? (
        <div className="empty">
          <p>No evidence has been uploaded.</p>
          <p className="small" style={{ marginTop: "0.4rem" }}>
            Upload what the client sends you. AuditLens reads it, then drafts findings
            for you to review.
          </p>
        </div>
      ) : (
        <ul className="doc-list">
          {documents.map((doc) => (
            <li key={doc.id} className="doc">
              <div className="doc-main">
                <a
                  href={`/api/evidence-documents/${doc.id}/download`}
                  className="doc-name"
                  download
                >
                  {doc.original_filename}
                </a>
                <div className="doc-meta tiny muted">
                  <span className="mono">{formatBytes(doc.size_bytes)}</span>
                  <span>·</span>
                  <span className="mono">{doc.created_at.slice(0, 10)}</span>
                  {doc.evidence_request_id && (
                    <>
                      <span>·</span>
                      <span>
                        against{" "}
                        <span className="clause">
                          {requests.find((r) => r.id === doc.evidence_request_id)?.control_id ??
                            "a request"}
                        </span>
                      </span>
                    </>
                  )}
                </div>

                {/* extraction_failed is a first-class state, not a footnote
                    (02_ARCHITECTURE.md §7.4). The file is still stored — it is
                    still evidence — and the message says what to do next. */}
                {doc.extraction_status === "extraction_failed" && (
                  <div className="note note-failed doc-note">
                    <strong className="tiny">Could not be read.</strong>{" "}
                    <span className="small">
                      {doc.extraction_error ??
                        "This document could not be processed."}{" "}
                      It is still stored as evidence — review it by hand.
                    </span>
                  </div>
                )}

                {doc.extraction_status === "complete" && doc.matching_status === "no_match" && (
                  <div className="note doc-note">
                    <span className="small">
                      Read successfully, but nothing in it matched the confirmed scope.
                      Check whether it belongs to this audit, or whether the scope
                      is missing a clause.
                    </span>
                  </div>
                )}

                {doc.matching_status === "deferred" && (
                  <div className="note note-attention doc-note">
                    <span className="small">
                      Read successfully. Matching is queued and will run when the
                      analysis service is back.
                    </span>
                  </div>
                )}
              </div>

              <div className="doc-status">
                <ExtractionPill status={doc.extraction_status} />
              </div>
            </li>
          ))}
        </ul>
      )}

      {!readOnly && (
        <form className="doc-upload" onSubmit={upload}>
          <div className="row wrap">
            <div className="grow">
              <label htmlFor="evidence_file" className="tiny">
                Upload a document
              </label>
              <input
                id="evidence_file"
                ref={fileInput}
                type="file"
                accept={ACCEPT}
                disabled={uploading}
                required
              />
            </div>

            {openRequests.length > 0 && (
              <div>
                <label htmlFor="link_request" className="tiny">
                  Against
                </label>
                <select
                  id="link_request"
                  value={linkTo}
                  onChange={(e) => setLinkTo(e.target.value)}
                  disabled={uploading}
                >
                  <option value="">No specific request</option>
                  {openRequests.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.control_id}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <button
              type="submit"
              className="btn btn-sm btn-primary"
              // Also disabled while the page re-fetches, so a second submit
              // cannot race the first upload's refresh.
              disabled={uploading || refreshing}
            >
              {uploading ? "Uploading…" : "Upload"}
            </button>
          </div>
          <p className="hint tiny">
            PDF, DOCX, XLSX, PNG or JPG, up to {MAX_MB}MB. Uploaded documents are kept
            permanently and cannot be deleted.
          </p>
        </form>
      )}
    </section>
  );
}

function ExtractionPill({ status }: { status: ExtractionStatus }) {
  if (status === "processing") {
    return <span className="pill pill-neutral">Reading…</span>;
  }
  if (status === "extraction_failed") {
    return <span className="pill pill-failed">Unreadable</span>;
  }
  return <span className="pill pill-satisfied">Read</span>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
