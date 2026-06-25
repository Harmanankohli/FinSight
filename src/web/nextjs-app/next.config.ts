import type { NextConfig } from "next";

const ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || "http://localhost:8001";

const nextConfig: NextConfig = {
  output: "standalone",
  env: {
    NEXT_PUBLIC_ORCHESTRATOR_URL: ORCHESTRATOR_URL,
  },
  async rewrites() {
    return [
      {
        source: "/api/orch/:path*",
        destination: `${ORCHESTRATOR_URL}/:path*`,
      },
      {
        source: "/auth/:path*",
        destination: `${ORCHESTRATOR_URL}/auth/:path*`,
      },
      {
        source: "/reports/:path*",
        destination: `${ORCHESTRATOR_URL}/reports/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/api/orch/:path*",
        headers: [
          { key: "Access-Control-Allow-Origin", value: "*" },
        ],
      },
    ];
  },
};

export default nextConfig;
