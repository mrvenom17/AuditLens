import { serverFetch } from "@/lib/server-api";
import { type ControlDefinitionResponse } from "@/types/api";

import "./corpus.css";

export const metadata = { title: "Rules Dictionary · AuditLens" };

export default async function CorpusPage() {
  const controls = await serverFetch<ControlDefinitionResponse[]>("/api/control-definitions");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Rules Dictionary</h1>
          <p className="page-sub">PCI DSS v4.0.1 control corpus and evaluation mechanics.</p>
        </div>
      </div>

      <div className="corpus-grid">
        {controls.length === 0 ? (
          <div className="panel empty">
            <p>No controls defined in the corpus yet.</p>
          </div>
        ) : (
          controls.map((c) => (
            <div className="panel control-card" key={c.id}>
              <div className="control-header">
                <div className="control-id">{c.control_id}</div>
                <div className="control-name">{c.name}</div>
                <span
                  className={
                    c.evaluation_mode === "DETERMINISTIC"
                      ? "pill pill-neutral"
                      : "pill pill-satisfied"
                  }
                >
                  {c.evaluation_mode === "DETERMINISTIC" ? "Deterministic" : "LLM Assisted"}
                </span>
              </div>
              <div className="control-body">
                <div className="requirement-text">
                  <strong>Requirement:</strong>
                  <p>{c.requirement_text}</p>
                </div>
                
                <div className="mechanics-grid">
                  <div className="mechanic-section">
                    <strong>Expected Facts</strong>
                    {c.facts && c.facts.length > 0 ? (
                      <ul className="fact-list">
                        {c.facts.map((f, i) => (
                          <li key={i}>
                            <code>{f.name}</code> <span className="small muted">({f.type})</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="small muted">None declared.</p>
                    )}
                  </div>
                  
                  <div className="mechanic-section">
                    <strong>Evaluation Rules</strong>
                    {c.rules && c.rules.length > 0 ? (
                      <ul className="rule-list">
                        {c.rules.map((r, i) => (
                          <li key={i}>
                            <code>{r.fact}</code> <strong>{r.operator}</strong>{" "}
                            {r.expected !== undefined && r.expected !== null && (
                              <code>{String(r.expected)}</code>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="small muted">None declared.</p>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </>
  );
}
