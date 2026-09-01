/**
 * Server-side API access for React Server Components.
 *
 * A server-rendered request has no browser attached, so the session cookie has
 * to be forwarded explicitly from the incoming request. Doing the fetch on the
 * server rather than in the client means a protected page never renders a
 * logged-out shell first and then corrects itself, which matters here: a flash
 * of an empty audit list looks exactly like "you have no audits".
 *
 * These calls go to the API service directly (API_INTERNAL_URL), not through
 * the Next rewrite — the rewrite exists to give the *browser* a same-origin
 * path, and the server has no such constraint.
 */

import { cookies } from "next/headers";

import { ApiError } from "@/lib/api";
import type { ApiErrorBody } from "@/types/api";

const INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

export async function serverFetch<T>(path: string): Promise<T> {
  const cookieStore = await cookies();
  const cookieHeader = cookieStore
    .getAll()
    .map((c) => `${c.name}=${c.value}`)
    .join("; ");

  const response = await fetch(`${INTERNAL_URL}${path}`, {
    headers: {
      Accept: "application/json",
      ...(cookieHeader ? { Cookie: cookieHeader } : {}),
    },
    // Audit data changes as the worker processes evidence, and an auditor
    // acting on a cached queue would be reviewing findings that have already
    // moved. Never cache.
    cache: "no-store",
  });

  if (!response.ok) {
    let body: ApiErrorBody["error"] | undefined;
    try {
      body = ((await response.json()) as ApiErrorBody).error;
    } catch {
      body = undefined;
    }
    throw new ApiError(
      response.status,
      body ?? {
        code: "UNEXPECTED_ERROR",
        message: `The server returned ${response.status}.`,
        request_id: "",
      },
    );
  }

  return (await response.json()) as T;
}

/** Fetch, or null when the caller is not authorized to see it.
 *
 * Used where the page wants to render a partial view rather than fail whole —
 * never to paper over a denial the user should be told about. */
export async function serverFetchOrNull<T>(path: string): Promise<T | null> {
  try {
    return await serverFetch<T>(path);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
      return null;
    }
    throw error;
  }
}
