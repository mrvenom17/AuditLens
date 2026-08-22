import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Standalone output keeps the production image small (09_DEPLOYMENT.md).
  output: "standalone",
  // 05_SECURITY.md §10.9 — the frontend sets its own headers; the API sets its
  // own in FastAPI middleware. Neither relies on the other.
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
