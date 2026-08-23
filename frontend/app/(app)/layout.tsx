import { redirect } from "next/navigation";

import { AppNav } from "@/components/AppNav";
import { ApiError } from "@/lib/api";
import { serverFetch } from "@/lib/server-api";
import type { CurrentUser } from "@/types/api";

import "./app-shell.css";

/**
 * The authenticated shell.
 *
 * Identity is re-derived from the server on every request rather than held in
 * client state. That is not just tidiness: the backend re-reads role and
 * is_active per request precisely so a deactivation or role change takes effect
 * immediately, and a client-side identity cache would reintroduce exactly the
 * staleness the backend goes out of its way to avoid
 * (03_DATA_MODEL.md → Session).
 *
 * 02_ARCHITECTURE.md §7.4: the frontend hides and disables UI by role, but that
 * is a courtesy to the user, never the security boundary. Every action behind
 * it is re-authorized server-side.
 */
export default async function AppLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  let user: CurrentUser;
  try {
    user = await serverFetch<CurrentUser>("/api/auth/me");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      redirect("/login");
    }
    throw error;
  }

  return (
    <div className="shell">
      <AppNav user={user} />
      <main className="shell-main">{children}</main>
    </div>
  );
}
