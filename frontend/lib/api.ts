/**
 * Browser-side API client.
 *
 * Every request goes to a relative `/api/...` path, never an absolute origin.
 * That is what makes the session cookie work: it is httpOnly and
 * SameSite=Strict (05_SECURITY.md §10.2), so the browser only attaches it to
 * same-origin requests. In production the Cloudflare Tunnel routes `/api/*` to
 * the API service; in development a Next rewrite proxies it. The browser sees
 * one origin either way, which is also why no CSRF token is needed
 * (05_SECURITY.md §10.5).
 */

import type { ApiErrorBody, FieldError } from "@/types/api";

/** A structured API failure. Carries the server's stable error code so callers
 *  can branch on it rather than on message text. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  readonly detail: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody["error"]) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id;
    this.detail = body;
  }

  /** Field-level validation detail, when the server supplied it. */
  get fields(): FieldError[] {
    const raw = this.detail.fields;
    return Array.isArray(raw) ? (raw as FieldError[]) : [];
  }

  /** A message suitable for showing a user.
   *
   * Field errors are flattened in because a bare "Request validation failed"
   * tells someone nothing about which input to fix. */
  get displayMessage(): string {
    const fields = this.fields;
    if (fields.length > 0) {
      return fields.map((f) => `${humanizeField(f.field)}: ${f.reason}`).join(". ");
    }
    return this.message;
  }
}

function humanizeField(field: string): string {
  return field.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

async function toError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as ApiErrorBody;
    if (body?.error?.code) {
      return new ApiError(response.status, body.error);
    }
  } catch {
    // A non-JSON error body means something upstream of the application
    // answered — a proxy, or the tunnel. Fall through to a generic message
    // rather than surfacing whatever HTML it returned.
  }
  return new ApiError(response.status, {
    code: "UNEXPECTED_ERROR",
    message: `The server returned ${response.status}.`,
    request_id: "",
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    // Same-origin is the default, but stating it makes the cookie contract
    // explicit at the call site.
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    throw await toError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),

  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),

  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),

  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),

  /** Multipart upload. Deliberately does not set Content-Type — the browser
   *  must set it, including the multipart boundary. */
  upload: <T>(path: string, form: FormData) =>
    request<T>(path, { method: "POST", body: form }),
};
