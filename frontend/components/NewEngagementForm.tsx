"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { EngagementDetail, EntityType, MerchantLevel } from "@/types/api";

/**
 * Engagement intake.
 *
 * The conditional rule — merchant level is required for merchants and must be
 * absent for service providers — is enforced server-side by the Pydantic
 * schema. It is mirrored here only so the person filling the form finds out
 * before they submit, never as the authority
 * (06_ENGINEERING_RULES.md § Validation).
 */
export function NewEngagementForm() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [clientName, setClientName] = useState("");
  const [entityType, setEntityType] = useState<EntityType>("merchant");
  const [merchantLevel, setMerchantLevel] = useState<MerchantLevel>("4");
  const [volume, setVolume] = useState("");
  const [saqType, setSaqType] = useState("");
  const [techStack, setTechStack] = useState("");

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      const created = await api.post<EngagementDetail>("/api/engagements", {
        client_name: clientName,
        entity_type: entityType,
        // Sent only when it applies; the server rejects a level on a service
        // provider rather than ignoring it.
        merchant_level: entityType === "merchant" ? merchantLevel : null,
        annual_transaction_volume: volume ? Number(volume) : null,
        existing_saq_type: saqType || null,
        tech_stack_summary: techStack || null,
      });
      router.push(`/engagements/${created.id}`);
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.displayMessage
          : "Could not reach the server. Try again.",
      );
      setSubmitting(false);
    }
  }

  if (!open) {
    return (
      <button type="button" className="btn btn-primary" onClick={() => setOpen(true)}>
        New engagement
      </button>
    );
  }

  return (
    <div className="panel form-panel">
      <div className="panel-head">
        <h2>New engagement</h2>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={() => setOpen(false)}
          disabled={submitting}
        >
          Cancel
        </button>
      </div>

      <form className="panel-body" onSubmit={onSubmit} noValidate>
        <div className="field">
          <label htmlFor="client_name">Client name</label>
          <input
            id="client_name"
            required
            maxLength={200}
            value={clientName}
            onChange={(e) => setClientName(e.target.value)}
            disabled={submitting}
          />
        </div>

        <div className="form-grid">
          <div className="field">
            <label htmlFor="entity_type">Entity type</label>
            <select
              id="entity_type"
              value={entityType}
              onChange={(e) => setEntityType(e.target.value as EntityType)}
              disabled={submitting}
            >
              <option value="merchant">Merchant</option>
              <option value="service_provider">Service provider</option>
            </select>
          </div>

          {entityType === "merchant" && (
            <div className="field">
              <label htmlFor="merchant_level">Merchant level</label>
              <select
                id="merchant_level"
                value={merchantLevel}
                onChange={(e) => setMerchantLevel(e.target.value as MerchantLevel)}
                disabled={submitting}
              >
                <option value="1">Level 1</option>
                <option value="2">Level 2</option>
                <option value="3">Level 3</option>
                <option value="4">Level 4</option>
              </select>
            </div>
          )}
        </div>

        <div className="form-grid">
          <div className="field">
            <label htmlFor="volume">Annual transactions</label>
            <input
              id="volume"
              type="number"
              min={0}
              inputMode="numeric"
              value={volume}
              onChange={(e) => setVolume(e.target.value)}
              disabled={submitting}
            />
            <p className="hint">Optional. Informs the suggested scope.</p>
          </div>

          <div className="field">
            <label htmlFor="saq_type">Existing SAQ type</label>
            <input
              id="saq_type"
              maxLength={20}
              placeholder="A, A-EP, D…"
              value={saqType}
              onChange={(e) => setSaqType(e.target.value)}
              disabled={submitting}
            />
            <p className="hint">Optional. If the client has one on file.</p>
          </div>
        </div>

        <div className="field">
          <label htmlFor="tech_stack">Technology summary</label>
          <textarea
            id="tech_stack"
            maxLength={5000}
            value={techStack}
            onChange={(e) => setTechStack(e.target.value)}
            disabled={submitting}
            placeholder="Payment flow, hosting, and anything that touches cardholder data."
          />
          <p className="hint">
            From the firm&rsquo;s own file on this client. AuditLens never contacts the
            client&rsquo;s systems.
          </p>
        </div>

        {error && (
          <div className="note note-failed" role="alert">
            {error}
          </div>
        )}

        <div className="row" style={{ marginTop: "0.9rem" }}>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={submitting || !clientName.trim()}
          >
            {submitting ? "Creating…" : "Create engagement"}
          </button>
        </div>
      </form>
    </div>
  );
}
