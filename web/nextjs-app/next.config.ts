import type { NextConfig } from "next";

const ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || "http://localhost:8001";

const nextConfig: NextConfig = {
  env: {
    NEXT_PUBLIC_ORCHESTRATOR_URL: ORCHESTRATOR_URL,
  },
  async rewrites() {
    return [
      {
        source: "/api/orch/:path*",
        destination: `${ORCHESTRATOR_URL}/:path*`,
      },
    ];
  },
};

export default nextConfig;
