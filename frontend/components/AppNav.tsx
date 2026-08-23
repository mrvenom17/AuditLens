"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { ROLE_LABELS, type CurrentUser } from "@/types/api";

/**
 * The top bar.
 *
 * The role is shown because in this product it determines what the person can
 * do — a Reviewer is the only one who can sign off, and knowing which hat you
 * are wearing matters when the same person may hold different roles on
 * different engagements at other firms. It is a label, not a permission.
 */
export function AppNav({ user }: { user: CurrentUser }) {
  const router = useRouter();
  const pathname = usePathname();
  const [signingOut, setSigningOut] = useState(false);

  async function signOut() {
    setSigningOut(true);
    try {
      await api.post("/api/auth/logout");
    } finally {
      // Navigate regardless. The server-side session is what matters, and if
      // the call failed the next request will be rejected anyway — leaving the
      // user staring at a dead session would be worse than an optimistic exit.
      router.push("/login");
      router.refresh();
    }
  }

  const links = [
    { href: "/engagements", label: "Engagements" },
    ...(user.role === "admin" ? [{ href: "/admin/users", label: "Users" }] : []),
  ];

  return (
    <header className="nav">
      <div className="nav-inner">
        <Link href="/engagements" className="nav-brand">
          AuditLens
        </Link>

        <nav className="nav-links" aria-label="Main">
          {links.map((link) => {
            const active = pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={active ? "nav-link nav-link-active" : "nav-link"}
                aria-current={active ? "page" : undefined}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <div className="nav-user">
          <span className="nav-name">{user.name}</span>
          <span className="pill pill-neutral">{ROLE_LABELS[user.role]}</span>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={signOut}
            disabled={signingOut}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
      </div>
    </header>
  );
}
