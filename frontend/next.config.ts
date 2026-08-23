import type { NextConfig } from "next";

const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Next 16 writes AGENTS.md and CLAUDE.md into the project root on dev start.
  // This repository documents itself in /docs; a tool-generated file competing
  // with that is drift waiting to happen.
  agentRules: false,
  // Standalone output keeps the production image small (09_DEPLOYMENT.md).
  output: "standalone",

  /**
   * Proxy `/api/*` to the API service.
   *
   * This is what lets the browser treat the API as same-origin. The session
   * cookie is httpOnly and SameSite=Strict (05_SECURITY.md §10.2), so a
   * cross-origin call from the frontend would simply not carry it — and
   * relaxing SameSite to work around that would give up the CSRF protection
   * 05_SECURITY.md §10.5 relies on it for.
   *
   * In production the Cloudflare Tunnel routes `/api/*` straight to the API and
   * Next never sees these requests; this rewrite is the development path and a
   * fallback for any deployment that puts everything behind the frontend. The
   * browser sees one origin either way, so the client code is identical.
   */
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_INTERNAL_URL}/api/:path*`,
      },
    ];
  },

  // 05_SECURITY.md §10.9. The API sets its own headers in FastAPI middleware;
  // neither side relies on the other to do it.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
