"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import "./login.css";
import type { LoginResult } from "@/types/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [retryAfter, setRetryAfter] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    setRetryAfter(null);

    try {
      await api.post<LoginResult>("/api/auth/login", { email, password });
      // The session lives in an httpOnly cookie the browser now holds. Nothing
      // about the user is kept in client state — every page re-derives it from
      // the server, so there is no stale identity to reuse
      // (02_ARCHITECTURE.md §7.4).
      router.push("/audits");
      router.refresh();
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "TOO_MANY_ATTEMPTS") {
          const seconds = caught.detail.retry_after;
          setRetryAfter(typeof seconds === "number" ? seconds : null);
        }
        setError(caught.displayMessage);
      } else {
        setError("Could not reach the server. Check your connection and try again.");
      }
      setSubmitting(false);
    }
  }

  return (
    <main className="login">
      <div className="login-card">
        <header className="login-head">
          <h1>AuditLens</h1>
          <p className="muted small">PCI DSS v4.0.1 assessment workspace</p>
        </header>

        <form onSubmit={onSubmit} noValidate>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
            />
          </div>

          {error && (
            <div
              className={retryAfter === null ? "note note-failed" : "note note-attention"}
              role="alert"
            >
              {/* The server returns one generic message for a wrong password and
                  for an unknown account, and this renders it verbatim. Adding
                  anything more helpful here would undo the anti-enumeration
                  property the API is careful to hold (01_REQUIREMENTS.md). */}
              {error}
              {retryAfter !== null && (
                <div className="tiny" style={{ marginTop: "0.3rem" }}>
                  Try again in {Math.ceil(retryAfter / 60)} minute
                  {Math.ceil(retryAfter / 60) === 1 ? "" : "s"}.
                </div>
              )}
            </div>
          )}

          <button
            type="submit"
            className="btn btn-primary login-submit"
            disabled={submitting || !email || !password}
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="login-foot tiny muted">
          Accounts are created by an administrator. There is no self-registration.
        </p>
      </div>
    </main>
  );
}
